"""当前用户的知识库、文档列表、配额及导入状态接口。"""
from fastapi import APIRouter, Query

from utils.knowledge_access import require_kb_permission
from utils.knowledge_store import MAX_BYTES, MAX_DOCUMENTS, ensure_default_kb
from utils.task_utils import list_tasks, TERMINAL
from utils.user_store import get_db
from web.api.auth_dependencies import CurrentUser

router = APIRouter(tags=["我的知识库"])


@router.get("/knowledge-bases")
def knowledge_bases(user: CurrentUser):
    """
    获取当前用户的默认私有知识库
    :param user: 通过认证依赖取得的当前用户记录
    :return: 包含知识库 ID、名称和拥有者权限的列表
    """
    kb = ensure_default_kb(user["_id"])
    return {"items": [{"id": kb["_id"], "name": kb["name"], "permission": "owner"}]}


@router.get("/knowledge-bases/{kb_id}/documents")
def documents(kb_id: str, user: CurrentUser, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    """
    分页查询知识库文档，并统计文档数量与原文件占用
    :param kb_id: 本次操作所属的知识库 ID
    :param user: 通过认证依赖取得的当前用户记录
    :param offset: 分页起始位置，从 0 开始
    :param limit: 最多返回的记录数量
    :return: 文档列表、总数及配额信息
    """
    # 1. 校验拥有者权限，在指定知识库范围内读取文档
    require_kb_permission(user["_id"], kb_id)
    db = get_db()
    docs = list(db.documents.find({"kb_id": kb_id}).sort("created_at", -1))
    items = []
    # 2. 分页展示文档，仅为已经发布的版本提供原文件下载地址
    for doc in docs[offset:offset + limit]:
        original = db.assets.find_one({"kb_id": kb_id, "document_id": doc["_id"], "kind": "original", "task_id": doc.get("active_task")}) if doc.get("active_task") else None
        items.append({"id": doc["_id"], "filename": doc["filename"], "status": doc["status"],
                      "size_bytes": doc["size_bytes"], "download_url": f'/assets/{original["_id"]}' if original else None})
    # 3. 配额统计包含导入预留，替换期间按新旧文件大小的较大值计入
    return {"items": items, "total": len(docs), "quota": {"max_documents": MAX_DOCUMENTS,
            "max_bytes": MAX_BYTES, "used_bytes": sum(max(d["size_bytes"], d.get("pending_size", 0)) for d in docs)}}


@router.get("/knowledge-bases/{kb_id}/status")
def knowledge_status(kb_id: str, user: CurrentUser):
    """
    查询当前用户在指定知识库中是否仍有导入任务
    :param kb_id: 本次操作所属的知识库 ID
    :param user: 通过认证依赖取得的当前用户记录
    :return: 包含 importing 布尔值的字典
    """
    require_kb_permission(user["_id"], kb_id)
    return {"importing": any(t["kb_id"] == kb_id and t["status"] not in TERMINAL
                             for t in list_tasks("import", user_id=user["_id"]))}
