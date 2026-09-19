"""每次历史操作明确传入用户和知识库；不兼容无归属全库读取。"""
from time import time

from bson import ObjectId

from utils.knowledge_access import AccessDenied, require_chat, require_kb_permission
from utils.user_store import get_db


def clear_history(session_id, *, user_id):
    """
    检查会话归属后删除消息及会话记录
    :param session_id: 聊天会话 ID
    :param user_id: 服务端确定的用户 ID
    :return: 实际删除的消息数量
    """
    db = get_db()
    if not db.chat_sessions.find_one({"_id": session_id, "user_id": user_id}):
        raise AccessDenied("会话不存在")
    result = db.chat_message.delete_many({"session_id": session_id, "user_id": user_id})
    db.chat_sessions.delete_one({"_id": session_id, "user_id": user_id})
    return result.deleted_count


def save_chat_message(session_id, role, text, rewritten_query="", item_names=None,
                      image_urls=None, message_id=None, sources=None, *, user_id, kb_id):
    """
    在指定用户、知识库和会话范围内新增或更新消息
    :param session_id: 聊天会话 ID
    :param role: 消息角色，如 user 或 assistant
    :param text: 消息正文
    :param rewritten_query: 用于检索的问题改写文本
    :param item_names: 关联产品名称列表
    :param image_urls: 答案所选私有图片地址
    :param message_id: 要更新的消息 ID，省略时新增
    :param sources: 回答引用的来源资料列表
    :param user_id: 服务端确定的用户 ID
    :param kb_id: 本次操作所属的知识库 ID
    :return: 消息记录 ID 字符串
    """
    # 1. 验证归属，新增和更新都携带完整的用户、知识库及会话范围
    require_chat(user_id, session_id, kb_id)
    scope = {"session_id": session_id, "user_id": user_id, "kb_id": kb_id}
    doc = {**scope, "role": role, "text": text, "rewritten_query": rewritten_query or "",
           "item_names": item_names or [], "image_urls": image_urls or [], "sources": sources or [], "ts": time()}
    if message_id:
        # 2. 更新必须同时匹配消息 ID 和范围，不能仅凭消息 ID 修改其他会话
        result = get_db().chat_message.update_one({"_id": ObjectId(message_id), **scope}, {"$set": doc})
        if not result.matched_count:
            raise AccessDenied("消息不存在")
        return message_id
    return str(get_db().chat_message.insert_one(doc).inserted_id)


def update_message_item_names(ids, item_names, *, user_id, kb_id, session_id):
    """
    在当前会话范围内批量更新消息关联的产品名称
    :param ids: 待更新消息 ID 列表
    :param item_names: 确认后的产品名称列表
    :param user_id: 服务端确定的用户 ID
    :param kb_id: 本次操作所属的知识库 ID
    :param session_id: 聊天会话 ID
    :return: 实际修改的消息数量
    """
    require_chat(user_id, session_id, kb_id)
    result = get_db().chat_message.update_many({"_id": {"$in": [ObjectId(i) for i in ids]},
        "user_id": user_id, "kb_id": kb_id, "session_id": session_id}, {"$set": {"item_names": item_names}})
    return result.modified_count


def get_recent_messages(session_id, limit=10, *, user_id, kb_id):
    """
    读取指定会话的最近消息并按时间正序返回
    :param session_id: 聊天会话 ID
    :param limit: 最多返回的记录数量
    :param user_id: 服务端确定的用户 ID
    :param kb_id: 本次操作所属的知识库 ID
    :return: 可直接作为多轮问答上下文的消息列表
    """
    require_chat(user_id, session_id, kb_id)
    # 先倒序取最近的 limit 条，再翻转为正序，保留对话的时间顺序。
    records = list(get_db().chat_message.find({"session_id": session_id, "user_id": user_id,
        "kb_id": kb_id}).sort([("ts", -1), ("_id", -1)]).limit(limit))
    return list(reversed(records))


def list_sessions(limit=50, *, user_id, kb_id):
    """
    按用户和知识库汇总会话标题、更新时间及消息数量
    :param limit: 最多返回的记录数量
    :param user_id: 服务端确定的用户 ID
    :param kb_id: 本次操作所属的知识库 ID
    :return: 按最后活动时间倒序排列的会话摘要列表
    """
    require_kb_permission(user_id, kb_id)
    # 先按归属过滤，再按会话汇总；首条消息作标题，最后一条时间决定侧栏排序。
    return list(get_db().chat_message.aggregate([
        {"$match": {"user_id": user_id, "kb_id": kb_id}},
        {"$sort": {"ts": 1, "_id": 1}},
        {"$group": {"_id": "$session_id", "title": {"$first": "$text"},
                    "updated_at": {"$last": "$ts"}, "message_count": {"$sum": 1}}},
        {"$sort": {"updated_at": -1}}, {"$limit": limit},
        {"$project": {"_id": 0, "session_id": "$_id", "title": 1, "updated_at": 1,
                      "message_count": 1, "kb_id": {"$literal": kb_id}}},
    ], maxTimeMS=3000))
