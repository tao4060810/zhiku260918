"""使用普通 PyMongo 集合；共享一个带超时的连接，首次业务访问时建索引。"""
import os
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from pymongo import MongoClient

_db = None
_lock = Lock()


def utcnow():
    """
    取得带 UTC 时区信息的当前时间
    :return: 用于账号和登录记录的 datetime 对象
    """
    return datetime.now(timezone.utc)


def get_db():
    """
    延迟创建 MongoDB 连接及索引，并在进程内复用数据库对象
    :return: 账号、知识库和历史消息共用的数据库对象
    """
    global _db
    with _lock:
        if _db is None:
            client = MongoClient(os.environ["MONGO_URL"], tz_aware=True,
                                 serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=3000)
            db = client[os.environ["MONGO_DB_NAME"]]
            # 唯一索引防止并发创建同名账号或重复的默认知识库。
            db.users.create_index("username", unique=True)
            db.auth_sessions.create_index("token_hash", unique=True)
            db.auth_sessions.create_index("user_id")
            # TTL 索引异步清理过期记录，是否允许登录仍由 authenticate 实时判断。
            db.auth_sessions.create_index("expires_at", expireAfterSeconds=0)
            db.knowledge_bases.create_index("owner_user_id", unique=True)
            db.documents.create_index([("kb_id", 1), ("file_title", 1)], unique=True)
            db.assets.create_index("object_key", unique=True)
            db.assets.create_index([("kb_id", 1), ("document_id", 1)])
            db.chat_sessions.create_index([("user_id", 1), ("kb_id", 1)])
            db.chat_message.create_index([("user_id", 1), ("session_id", 1), ("ts", -1), ("_id", -1)])
            _db = db
        return _db


def public_user(user):
    """
    提取允许返回给前端的账号信息
    :param user: 通过认证依赖取得的当前用户记录
    :return: 只含用户 ID、用户名和角色的字典
    """
    return {"id": user["_id"], "username": user["username"], "role": user["role"]}


def create_user(username, password_hash, role="user"):
    """
    保存账号并确保其默认知识库存在
    :param username: 已规范化的用户名
    :param password_hash: 已计算好的密码摘要，不是明文密码
    :param role: 账号角色，公开注册使用默认 user
    :return: 新建的完整用户记录
    """
    user = {"_id": str(uuid4()), "username": username, "password_hash": password_hash,
            "role": role, "is_active": True, "auth_version": 1,
            "created_at": utcnow(), "updated_at": utcnow()}
    get_db().users.insert_one(user)
    from utils.knowledge_store import ensure_default_kb
    ensure_default_kb(user["_id"])
    return user
