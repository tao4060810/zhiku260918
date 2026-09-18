import os
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from utils.task_utils import (
    TaskConflict, add_done_task, create_task, get_task, list_tasks, update_task_status,
)
from web.api.workflows import run_import_graph

router = APIRouter(tags=["文档导入"])
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_FILES = 10


def data_root():
    return Path(os.getenv("DATA_BASED_ROOT_DIR") or Path(__file__).resolve().parents[2] / "output" / "data")


def safe_filename(name):
    if not name or len(name) > 180 or name != name.strip() or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise HTTPException(400, "文件名包含不支持的字符。")
    path = Path(name)
    if not path.stem or len(path.stem.encode("utf-8")) > 90 or path.suffix.lower() not in {".pdf", ".md"}:
        raise HTTPException(400, "请选择 PDF 或 Markdown 文件，文件名不超过 90 字节。")
    if path.stem.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}:
        raise HTTPException(400, "文件名为系统保留名称，请重命名。")
    return name


@router.post("/upload", status_code=202)
def upload_files(request: Request, files: list[UploadFile] = File(...)):
    if not 1 <= len(files) <= MAX_FILES:
        raise HTTPException(400, "每次请选择 1 至 10 个文件。")
    names = [safe_filename(f.filename) for f in files]
    staged = []
    task_ids = []
    try:
        # 提交处理任务前先校验整批文件，避免部分文件无效时仍启动导入。
        for upload, name in zip(files, names):
            folder = data_root() / datetime.now().strftime("%Y%m%d") / str(uuid4())
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
        for path in staged:
            task_id = create_task("import", filename=path.name)
            task_ids.append(task_id)
            add_done_task(task_id, "upload_file")
    except Exception as exc:
        for task_id in task_ids:
            update_task_status(task_id, "failed", error="批量上传未完成，请重试。")
        for path in staged:
            path.unlink(missing_ok=True)
            path.parent.rmdir()
        if isinstance(exc, TaskConflict):
            raise HTTPException(409, str(exc)) from None
        raise
    finally:
        for upload in files:
            upload.file.close()
    for task_id, path in zip(task_ids, staged):
        request.app.state.executor.submit(run_import_graph, task_id, path)
    return {"code": 200, "message": "文件已接收", "task_ids": task_ids}


@router.get("/status/{task_id}")
def get_task_progress(task_id: str):
    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在或已过期。")
    return {"code": 200, **task}


@router.get("/tasks")
def get_import_tasks():
    return {"items": list_tasks("import")}
