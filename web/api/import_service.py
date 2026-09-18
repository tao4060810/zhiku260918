# 文档导入接口：接收并校验文件、提交后台任务，以及提供导入进度查询。
import os
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4
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
def upload_files(request: Request, files: list[UploadFile] = File(...)):
    """
    批量上传文档，校验通过后提交后台导入任务
    :param request: 请求对象，用于获取应用共享的后台线程池
    :param files: 表单中名为 files 的上传文件列表
    :return: 接收提示及任务 ID 列表；HTTP 202 表示已接收，不代表入库完成
    """
    # 1. 校验本批文件数量和文件名
    if not 1 <= len(files) <= MAX_FILES:
        raise HTTPException(400, "每次请选择 1 至 10 个文件。")
    names = [safe_filename(f.filename) for f in files]
    staged = []  # 本批已写入磁盘的文件，失败时用于清理。
    task_ids = []  # 本批已创建的任务，整批准备成功后才提交到线程池。
    try:
        # 2. 保存并校验整批文件，避免部分文件无效时仍启动导入
        for upload, name in zip(files, names):
            # 每份文件使用独立目录，避免不同上传请求中的同名文件相互覆盖。
            folder = data_root() / datetime.now().strftime("%Y%m%d") / str(uuid4())
            folder.mkdir(parents=True, exist_ok=False)
            path = folder / name
            staged.append(path)
            size = 0
            with path.open("wb") as target:
                # 每次读取 1 MB，并累计检查大小，避免将整个上传文件一次读入内存。
                while chunk := upload.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise HTTPException(413, "单个文件不能超过 50 MB。")
                    target.write(chunk)
            if not size:
                raise HTTPException(400, "不能上传空文件。")
            if path.suffix.lower() == ".md":
                # UTF-8-SIG 同时兼容带 BOM 和不带 BOM 的 UTF-8 文本。
                try:
                    if not path.read_text(encoding="utf-8-sig").strip():
                        raise HTTPException(400, "Markdown 文件内容为空。")
                except UnicodeDecodeError:
                    raise HTTPException(400, "Markdown 文件需要使用 UTF-8 编码。") from None
            else:
                # 检查文件头中的 PDF 标记，完整解析由后续工作流完成。
                with path.open("rb") as source:
                    if b"%PDF-" not in source.read(1024):
                        raise HTTPException(400, "文件内容不是有效的 PDF。")
        # 3. 为校验通过的文件分别创建任务，标记上传步骤完成
        for path in staged:
            # 只将创建任务时的校验异常转换为冲突提示，文件处理异常仍由原流程处理。
            try:
                task_id = create_task("import", filename=path.name)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            task_ids.append(task_id)
            add_done_task(task_id, "upload_file")
    except Exception:
        # 准备阶段失败时回滚整批：标记已创建任务失败，并清理本次保存的文件。
        for task_id in task_ids:
            update_task_status(task_id, "failed", error="批量上传未完成，请重试。")
        for path in staged:
            path.unlink(missing_ok=True)
            path.parent.rmdir()
        raise
    finally:
        # 无论准备阶段成功还是失败，都释放上传文件的读取资源。
        for upload in files:
            upload.file.close()
    # 4. 提交后台导入任务并返回任务 ID，前端据此查询进度
    for task_id, path in zip(task_ids, staged):
        request.app.state.executor.submit(run_import_graph, task_id, path)
    return {"code": 200, "message": "文件已接收", "task_ids": task_ids}


@router.get("/status/{task_id}")
def get_task_progress(task_id: str):
    """
    查询单个任务的进度，支持导入和问答任务
    :param task_id: 要查询的任务 ID
    :return: 任务状态、节点进度、错误信息及处理结果
    """
    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在或已过期。")
    return {"code": 200, **task}


@router.get("/tasks")
def get_import_tasks():
    """
    查询当前进程保留的导入任务，供文档列表和导入进度弹窗展示
    :return: 包含导入任务列表的字典，按创建顺序从新到旧排列
    """
    return {"items": list_tasks("import")}
