"""第一阶段只比较拥有者；不读取成员表，不给管理员内容访问特权。"""
import json
from uuid import UUID

from utils.user_store import get_db


class AccessDenied(Exception):
    """资源不存在或归属不匹配时抛出，Web 层统一返回不可访问提示。"""
    pass


def valid_id(value):
    """
    检查输入是否是标准格式的 UUID 字符串
    :param value: 待校验的标识
    :return: 格式有效返回 True，否则返回 False
    """
    try:
        return str(UUID(str(value))) == value
    except (ValueError, TypeError, AttributeError):
        return False


def require_kb_permission(user_id, kb_id, action="read"):
    """
    检查账号有效性和私有知识库拥有者权限
    :param user_id: 服务端确定的用户 ID
    :param kb_id: 本次操作所属的知识库 ID
    :param action: 所需操作，目前仅支持 read 或 upload
    :return: 校验通过的知识库记录
    """
    # 1. 限定操作类型与标识格式，缺少范围时直接拒绝
    if action not in {"read", "upload"} or not valid_id(user_id) or not valid_id(kb_id):
        raise AccessDenied("知识库不存在或无权访问")
    db = get_db()
    if not db.users.find_one({"_id": user_id, "is_active": True}):
        raise AccessDenied("账号不可用")
    # 2. 当前版本只允许拥有者访问，管理员角色也不能绕过内容归属检查
    kb = db.knowledge_bases.find_one({"_id": kb_id, "owner_user_id": user_id, "status": "active"})
    if not kb:
        raise AccessDenied("知识库不存在或无权访问")
    return kb


def require_chat(user_id, session_id, kb_id=None):
    """
    检查会话属于当前用户，并继续校验对应知识库权限
    :param user_id: 服务端确定的用户 ID
    :param session_id: 聊天会话 ID
    :param kb_id: 可选的知识库范围，指定时会话必须属于该库
    :return: 校验通过的会话记录
    """
    query = {"_id": session_id, "user_id": user_id}
    if kb_id is not None:
        query["kb_id"] = kb_id
    chat = get_db().chat_sessions.find_one(query)
    if not chat:
        raise AccessDenied("会话不存在或无权访问")
    require_kb_permission(user_id, chat["kb_id"])
    return chat


def kb_filter(kb_id, *, document_id=None, item_names=None):
    """
    构建带知识库范围的 Milvus 过滤表达式
    :param kb_id: 本次操作所属的知识库 ID
    :param document_id: 可选文档 ID
    :param item_names: 可选产品名称列表
    :return: 按知识库、可选文档和产品名组合的过滤字符串
    """
    if not valid_id(kb_id) or (document_id is not None and not valid_id(document_id)):
        raise AccessDenied("缺少有效知识库范围")
    # JSON 编码负责字符串转义，不能直接把用户输入拼接成过滤条件。
    expr = f'kb_id == {json.dumps(kb_id)}'
    if document_id is not None:
        expr += f' and document_id == {json.dumps(document_id)}'
    if item_names:
        expr += f' and item_name in {json.dumps(item_names, ensure_ascii=False)}'
    return expr


def search_filter(kb_id, *, item_names=None):
    # 只有完成导入、由 MongoDB 发布的版本可进入检索。中断的半成品默认不可见。
    """
    在知识库过滤条件上限定已成功发布的导入版本
    :param kb_id: 本次操作所属的知识库 ID
    :param item_names: 可选产品名称列表
    :return: 只允许检索 active_task 所指版本的过滤字符串
    """
    expr = kb_filter(kb_id, item_names=item_names)
    versions = [d["active_task"] for d in get_db().documents.find({"kb_id": kb_id, "active_task": {"$exists": True}})]
    return expr + f' and import_task in {json.dumps(versions or ["unpublished"])}'


def check_local_docs(docs, kb_id, *, hits=False):
    """
    核验检索结果中的本地资料仍属于本次知识库
    :param docs: 待检查的候选资料或搜索命中列表
    :param kb_id: 本次操作所属的知识库 ID
    :param hits: True 表示按 Milvus 命中结构读取 entity 字段
    :return: 通过校验的原资料列表
    """
    for doc in docs:
        entity = doc.get("entity", {}) if hits else doc
        if (hits or entity.get("source") == "local") and (
            entity.get("kb_id") != kb_id or not valid_id(entity.get("document_id"))
        ):
            raise AccessDenied("检索返回了范围外的数据")
    return docs
