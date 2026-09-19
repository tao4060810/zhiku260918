from web.api.auth_dependencies import CurrentUser
from utils.knowledge_access import require_kb_permission
from utils.knowledge_store import mutation_lock, reserve_documents, rollback_documents, finish_document
from utils.task_utils import TERMINAL, MAX_ACTIVE_TASKS, finish_task, cancel_task, cancel_upload, is_upload_canceled
from utils.user_store import get_db
# 文档导入接口：接收并校验文件、提交后台任务，以及提供导入进度查询。
import os
import re
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from utils.task_utils import (
    add_done_task, create_task, get_task, list_tasks, update_task_status,
)
from web.api.workflows import run_import_graph

router = APIRouter(tags=["文档导入"])
MAX_FILE_BYTES = 50 * 1024 * 1024  # 单个文件最多 50 MB。
MAX_FILES = 10  # 每批最多上传 10 个文件。


def data_root():
    """
    获取上传文件的本地存储根目录
    :return: 存储目录的 Path 对象
    """
    # 优先使用环境变量指定的存储目录，未配置时使用项目下的 output/data。
    return Path(os.getenv("DATA_BASED_ROOT_DIR") or Path(__file__).resolve().parents[2] / "output" / "data")


def safe_filename(name):
    """
    校验上传文件名，不符合要求时抛出 HTTP 400 异常
    :param name: 上传文件的原始名称，包含扩展名
    :return: 校验通过的原始文件名
    """
    # 1. 检查空名称、长度和非法字符，拒绝路径分隔符
    if not name or len(name) > 180 or name != name.strip() or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise HTTPException(400, "文件名包含不支持的字符。")
    path = Path(name)
    # 2. 检查文件类型和不含扩展名的文件名长度，仅支持 PDF 和 Markdown
    if not path.stem or len(path.stem.encode("utf-8")) > 90 or path.suffix.lower() not in {".pdf", ".md"}:
        raise HTTPException(400, "请选择 PDF 或 Markdown 文件，文件名不超过 90 字节。")
    # 3. 拒绝 Windows 系统保留名称，即使带有扩展名也不能使用
    if path.stem.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}:
        raise HTTPException(400, "文件名为系统保留名称，请重命名。")
    return name


@router.post("/upload", status_code=202)
def upload_files(request: Request, user: CurrentUser, kb_id: str, files: list[UploadFile] = File(...), upload_id: UUID | None = None):
    """
    校验私有知识库权限，保存整批文件并提交后台导入
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param user: 通过认证依赖取得的当前用户记录
    :param kb_id: 本次操作所属的知识库 ID
    :param files: 表单中名为 files 的上传文件列表
    :param upload_id: 可选的客户端批次 UUID，用于在上传响应返回前关联取消请求
    :return: HTTP 202 响应中的任务 ID 列表，不代表解析入库已完成
    """
    # 1. 从认证依赖取得用户身份，校验其对目标知识库的上传权限
    require_kb_permission(user["_id"], kb_id, "upload")
    upload_id = str(upload_id) if upload_id else None
    staged, task_ids, reservations = [], [], []
    try:
        # 取消请求可能先到达；此时直接拒绝本批文件，不预留文档或创建任务。
        if is_upload_canceled(user["_id"], upload_id):
            raise HTTPException(409, "上传已终止。")
        if not 1 <= len(files) <= MAX_FILES:
            raise HTTPException(400, "每次请选择 1 至 10 个文件。")
        names = [safe_filename(f.filename) for f in files]
        # 2. 整批文件先保存到知识库的独立暂存目录，逐份检查大小及内容格式
        for upload, name in zip(files, names):
            folder = data_root() / kb_id / "staging" / str(uuid4())
            folder.mkdir(parents=True, exist_ok=False)
            path = folder / name
            staged.append(path)
            size = 0
            with path.open("wb") as target:
                while chunk := upload.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise HTTPException(413, "单个文件不能超过 50 MB。")
                    target.write(chunk)
            if not size:
                raise HTTPException(400, "不能上传空文件。")
            if path.suffix.lower() == ".md":
                try:
                    if not path.read_text(encoding="utf-8-sig").strip():
                        raise HTTPException(400, "Markdown 文件内容为空。")
                except UnicodeDecodeError:
                    raise HTTPException(400, "Markdown 文件需要使用 UTF-8 编码。") from None
            else:
                with path.open("rb") as source:
                    if b"%PDF-" not in source.read(1024):
                        raise HTTPException(400, "文件内容不是有效的 PDF。")
        # 3. 在同一把锁内检查整批容量、预留文档并创建任务，准备完成后才提交后台
        with mutation_lock:
            try:
                # 暂存文件期间也可能收到取消；在创建任务的同一把锁内再次检查。
                if is_upload_canceled(user["_id"], upload_id):
                    raise HTTPException(409, "上传已终止。")
                active = [t for t in list_tasks() if t["status"] not in TERMINAL]
                own = [t for t in active if t["user_id"] == user["_id"] and t["kind"] == "import"]
                if len(active) + len(files) > MAX_ACTIVE_TASKS or len(own) + len(files) > 10:
                    raise ValueError("任务队列已满，请稍后再试。")
                reservations = reserve_documents(user["_id"], kb_id, [(p.name, p.stat().st_size) for p in staged])
                for i, (entry, path) in enumerate(zip(reservations, staged)):
                    task_id = create_task("import", user_id=user["_id"], kb_id=kb_id, filename=path.name,
                                          document_id=entry["document_id"], size_bytes=entry["size"], upload_id=upload_id)
                    task_ids.append(task_id)
                    # 路径包含知识库、文档和任务 ID，使每次导入的本地产物互相隔离。
                    target = data_root() / kb_id / entry["document_id"] / task_id / path.name
                    target.parent.mkdir(parents=True, exist_ok=False)
                    path.replace(target)
                    path.parent.rmdir()
                    staged[i] = target
                    get_db().documents.update_one({"_id": entry["document_id"], "kb_id": kb_id},
                                                  {"$set": {"pending_task": task_id}})
                    add_done_task(task_id, "upload_file")
            except Exception:
                # 任务创建失败时恢复整批文档原记录，已创建任务统一标记失败。
                for task_id in task_ids:
                    finish_task(task_id, error="批量上传未完成，请重试。")
                rollback_documents(kb_id, reservations)
                raise
    except Exception as exc:
        # 清理本批保存的文件，将配额或任务冲突转换为前端可读的 HTTP 409。
        for path in staged:
            path.unlink(missing_ok=True)
            if path.parent.exists():
                path.parent.rmdir()
        if isinstance(exc, ValueError):
            raise HTTPException(409, str(exc)) from None
        raise
    finally:
        for upload in files:
            upload.file.close()
    # 4. 提交后台任务；线程池已关闭时释放预留，避免文档永久停留在处理中
    for task_id, path in zip(task_ids, staged):
        try:
            request.app.state.executor.submit(run_import_graph, task_id, path)
        except RuntimeError:
            finish_document(get_task(task_id), False)
            finish_task(task_id, error="服务正在关闭，导入未开始。")
    return {"code": 200, "message": "文件已接收", "task_ids": task_ids}


@router.get("/status/{task_id}")
def get_task_progress(task_id: str, user: CurrentUser):
    """
    查询当前用户拥有的任务，拒绝访问其他用户的任务
    :param task_id: 后台任务 ID
    :param user: 通过认证依赖取得的当前用户记录
    :return: 任务状态、节点进度、错误及处理结果
    """
    task = get_task(task_id)
    if not task or task["user_id"] != user["_id"]:
        raise HTTPException(404, "任务不存在或已过期。")
    require_kb_permission(user["_id"], task["kb_id"])
    return {"code": 200, **task}


@router.post("/uploads/{upload_id}/cancel", status_code=204)
def cancel_upload_batch(upload_id: UUID, user: CurrentUser):
    """
    取消当前用户的上传批次，允许取消时后台尚未创建任务
    :param upload_id: 与上传请求相同的批次 UUID
    :param user: 通过认证依赖取得的当前用户记录，写请求同时校验 CSRF
    :return: HTTP 204 表示已记录取消，不表示正在运行的任务已完成清理
    """
    try:
        cancel_upload(user["_id"], str(upload_id))
    except ValueError as exc:
        raise HTTPException(429, str(exc)) from None


@router.post("/tasks/{task_id}/cancel")
def cancel_import_task(task_id: str, user: CurrentUser):
    """
    请求终止本人有上传权限的导入任务，不撤销已完成的文件
    :param task_id: 要终止的导入任务 ID，不接受问答任务
    :param user: 通过认证依赖取得的当前用户记录
    :return: 当前任务状态；取消请求被接受后由后台收尾并进入 canceled
    """
    task = get_task(task_id)
    if not task or task["user_id"] != user["_id"] or task["kind"] != "import":
        raise HTTPException(404, "任务不存在或已过期。")
    require_kb_permission(user["_id"], task["kb_id"], "upload")
    return {"code": 200, **(cancel_task(task_id) or task)}


@router.get("/tasks")
def get_import_tasks(user: CurrentUser, kb_id: str | None = None):
    """
    查询当前用户的导入任务，可按知识库进一步筛选
    :param user: 通过认证依赖取得的当前用户记录
    :param kb_id: 可选的知识库 ID，指定时额外校验知识库权限
    :return: 按创建顺序从新到旧排列的任务列表
    """
    if kb_id:
        require_kb_permission(user["_id"], kb_id)
    items = list_tasks("import", user_id=user["_id"])
    return {"items": [t for t in items if kb_id is None or t["kb_id"] == kb_id]}
