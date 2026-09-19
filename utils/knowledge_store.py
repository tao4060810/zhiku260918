"""文档元数据；单进程共用任务锁协调配额、任务预留和会话删除。"""
import mimetypes
import os
from pathlib import Path
from uuid import uuid4
from pymongo.errors import DuplicateKeyError

from utils.knowledge_access import AccessDenied, require_kb_permission
from utils.task_utils import _lock as mutation_lock
from utils.user_store import get_db, utcnow

MAX_DOCUMENTS = int(os.getenv("KB_MAX_DOCUMENTS", "100"))
MAX_BYTES = int(os.getenv("KB_MAX_ORIGINAL_BYTES", str(1024 ** 3)))


def recover_interrupted_imports():
    """
    恢复上次进程中断遗留的导入预留，由 Web 启动或首次认证请求调用
    管理命令仅连接数据库时不调用，避免清理仍在执行的导入
    """
    db = get_db()
    # 新建文档尚未发布时直接删除占位；替换文档则保留旧版本并释放本次预留。
    db.documents.delete_many({"pending_size": {"$exists": True}, "size_bytes": 0, "active_task": {"$exists": False}})
    db.documents.update_many({"pending_size": {"$exists": True}}, {
        "$set": {"status": "failed"}, "$unset": {"pending_task": "", "pending_size": ""}})


def ensure_default_kb(user_id):
    """
    为用户创建或读取唯一的默认私有知识库
    :param user_id: 服务端确定的用户 ID
    :return: 默认知识库记录
    """
    db = get_db()
    try:
        db.knowledge_bases.update_one({"owner_user_id": user_id}, {"$setOnInsert": {
            "_id": str(uuid4()), "name": "我的知识库", "status": "active", "created_at": utcnow()
        }}, upsert=True)
    except DuplicateKeyError:
        pass  # 另一登录请求已经创建默认库。
    return db.knowledge_bases.find_one({"owner_user_id": user_id})


def reserve_documents(user_id, kb_id, files):
    """
    检查整批上传的配额并预留文档记录，调用方须持有 mutation_lock
    :param user_id: 当前上传用户 ID
    :param kb_id: 目标知识库 ID
    :param files: 文件名与字节数组成的二元组列表
    :return: 含原记录快照的预留列表，用于任务创建和失败回滚
    """
    # 1. 校验上传权限，读取现有文档，拒绝同批重复标题
    require_kb_permission(user_id, kb_id, "upload")
    db = get_db()
    docs = list(db.documents.find({"kb_id": kb_id}))
    titles = [Path(name).stem for name, _ in files]
    if len(set(titles)) != len(titles):
        raise ValueError("同一批次不能上传标题相同的文件。")
    by_title = {d["file_title"]: d for d in docs}
    # 2. 计算已有占用和替换所需增量，预留期间不能通过并发上传绕过配额
    used = sum(max(d.get("size_bytes", 0), d.get("pending_size", 0)) for d in docs)
    count = len(docs)
    for (name, size), title in zip(files, titles):
        old = by_title.get(title)
        if old and old.get("pending_task"):
            raise ValueError("同名文档仍在处理中。")
        count += int(old is None)
        used += max(0, size - (old or {}).get("size_bytes", 0))
    if count > MAX_DOCUMENTS or used > MAX_BYTES:
        raise ValueError("知识库文档数量或原文件容量已达上限。")
    # 3. 保存旧记录快照后写入预留；任何一份失败都回滚本批已写入的记录
    reservations = []
    try:
        for (name, size), title in zip(files, titles):
            old = by_title.get(title)
            document_id = old["_id"] if old else str(uuid4())
            reservations.append({"document_id": document_id, "old": old, "size": size, "filename": name})
            db.documents.update_one({"_id": document_id, "kb_id": kb_id}, {
                "$set": {"filename": name, "file_title": title, "uploaded_by": user_id,
                         "pending_size": size, "status": "pending", "updated_at": utcnow()},
                "$setOnInsert": {"size_bytes": 0, "created_at": utcnow()},
            }, upsert=True)
        return reservations
    except Exception:
        rollback_documents(kb_id, reservations)
        raise


def rollback_documents(kb_id, reservations):
    """
    回滚本批文档预留，旧文档恢复原记录，新文档删除占位
    :param kb_id: 本次操作所属的知识库 ID
    :param reservations: reserve_documents 返回的预留记录列表
    """
    for entry in reservations:
        query = {"_id": entry["document_id"], "kb_id": kb_id}
        if entry["old"]:
            get_db().documents.replace_one(query, entry["old"])
        else:
            get_db().documents.delete_one(query)


def finish_document(task, success):
    """
    结束文档导入，成功时发布新版本，失败时释放预留
    :param task: 含知识库、文档、任务 ID 和文件大小的任务记录
    :param success: 是否成功完成本次导入
    """
    # 1. 只更新仍由本次任务占用的预留，避免影响同文档的后续任务
    with mutation_lock:
        values = {"status": "ready" if success else "failed", "updated_at": utcnow()}
        query = {"_id": task["document_id"], "kb_id": task["kb_id"], "pending_task": task["task_id"]}
        if success:
            # 2. 成功后切换 active_task，检索过滤器从此只读取新发布的版本
            values["size_bytes"] = task["size_bytes"]
            values["active_task"] = task["task_id"]
        elif get_db().documents.find_one({**query, "size_bytes": 0}):
            # 新文档导入失败时删除空占位；已有旧版本的文档则保留原数据。
            get_db().documents.delete_one(query)
            return
        get_db().documents.update_one(query, {
            "$set": values, "$unset": {"pending_task": "", "pending_size": ""},
        })


def store_asset(user_id, kb_id, document_id, task_id, path, kind):
    """
    上传原文件或图片到私有桶，并登记资产元数据
    :param user_id: 服务端确定的用户 ID
    :param kb_id: 本次操作所属的知识库 ID
    :param document_id: 知识库中的文档 ID
    :param task_id: 后台任务 ID
    :param path: 待上传文件的本地路径
    :param kind: 资产类型，原文件为 original，图片为 image
    :return: 需要鉴权的 /assets/资产ID 地址
    """
    # 1. 确认文档属于目标知识库，图片仅接受允许的 MIME 类型
    require_kb_permission(user_id, kb_id, "upload")
    if not get_db().documents.find_one({"_id": document_id, "kb_id": kb_id}):
        raise AccessDenied("文档不存在")
    from config.minio_config import minio_config
    from utils.minio_utils import get_minio_client
    asset_id = str(uuid4())
    path = Path(path)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if kind == "image" and content_type not in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}:
        raise ValueError("不支持的图片类型")
    # 2. 对象路径包含知识库、文档和导入任务，避免不同版本或用户相互覆盖
    key = f"knowledge/{kb_id}/documents/{document_id}/imports/{task_id}/{asset_id}{path.suffix.lower()}"
    client = get_minio_client()
    client.fput_object(minio_config.bucket_name, key, str(path), content_type=content_type)
    record = {"_id": asset_id, "kb_id": kb_id, "document_id": document_id, "task_id": task_id,
              "kind": kind, "bucket": minio_config.bucket_name, "object_key": key,
              "filename": path.name, "size_bytes": path.stat().st_size, "content_type": content_type,
              "created_at": utcnow()}
    # 3. 上传成功后登记元数据；登记失败则删除刚上传的对象
    try:
        get_db().assets.insert_one(record)
    except Exception:
        client.remove_object(minio_config.bucket_name, key)
        raise
    return f"/assets/{asset_id}"


def clean_failed_assets(task):
    """
    清理失败导入任务创建的私有对象和资产记录
    :param task: 指定知识库、文档及导入任务范围的任务记录
    """
    from utils.minio_utils import get_minio_client
    query = {"kb_id": task["kb_id"], "document_id": task["document_id"], "task_id": task["task_id"]}
    for asset in get_db().assets.find(query):
        get_minio_client().remove_object(asset["bucket"], asset["object_key"])
        get_db().assets.delete_one({"_id": asset["_id"], **query})
