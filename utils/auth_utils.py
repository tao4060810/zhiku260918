"""密码由 Argon2 处理；Cookie 是随机凭证，MongoDB 保存其摘要。"""
import hashlib
import secrets
from datetime import timedelta

from pwdlib import PasswordHash

from config.auth_config import auth_config
from utils.user_store import get_db, utcnow

passwords = PasswordHash.recommended()
DUMMY_HASH = passwords.hash(secrets.token_urlsafe(32))  # 用户不存在时仍进行一次密码摘要校验。


class LoginRequired(Exception):
    """登录无效时交给 Web 层统一处理为页面跳转或 HTTP 401。"""
    pass


def token_digest(token):
    """
    计算登录凭证的 SHA-256 摘要，数据库不保存原始凭证
    :param token: 浏览器 Cookie 中的原始随机登录凭证
    :return: 用于查询登录记录的十六进制摘要
    """
    return hashlib.sha256(token.encode()).hexdigest()


def issue_session(user):
    """
    创建随机登录凭证并保存登录有效期和 CSRF 令牌
    :param user: 通过认证依赖取得的当前用户记录
    :return: 原始凭证及已保存的登录记录
    """
    # 1. 分别生成登录凭证和 CSRF 令牌，数据库仅保存登录凭证的摘要
    raw = secrets.token_urlsafe(32)
    now = utcnow()
    record = {"token_hash": token_digest(raw), "user_id": user["_id"],
              "auth_version": user["auth_version"], "csrf_token": secrets.token_urlsafe(32),
              "created_at": now, "last_seen_at": now,
              "expires_at": now + timedelta(seconds=auth_config.ttl), "revoked_at": None}
    # 2. 保存绝对过期时间和认证版本，后续请求同时检查是否过期或被撤销
    get_db().auth_sessions.insert_one(record)
    return raw, record


def authenticate(token, *, touch=True):
    """
    检查登录记录、绝对有效期、空闲有效期及用户状态
    :param token: 浏览器 Cookie 中的原始随机登录凭证
    :param touch: 是否刷新最后活动时间；后台复查时传 False
    :return: 有效用户记录和登录记录；失败时抛出 LoginRequired
    """
    # 1. 校验凭证长度，再查询尚未撤销的登录记录
    if not token or not 32 <= len(token) <= 128:
        raise LoginRequired()
    db = get_db()
    record = db.auth_sessions.find_one({"token_hash": token_digest(token), "revoked_at": None})
    now = utcnow()
    # 2. 同时检查绝对期限和空闲期限，不依赖 MongoDB 的延迟过期清理
    if not record or record["expires_at"] <= now or record["last_seen_at"] + timedelta(seconds=auth_config.idle) <= now:
        raise LoginRequired()
    # 3. 禁用账号或修改认证版本后，已有凭证即使尚未过期也不能继续使用
    user = db.users.find_one({"_id": record["user_id"], "is_active": True, "auth_version": record["auth_version"]})
    if not user:
        raise LoginRequired()
    if touch:
        db.auth_sessions.update_one({"_id": record["_id"], "revoked_at": None}, {"$set": {"last_seen_at": now}})
    return user, record


def revoke_session(token):
    """
    将指定登录凭证标记为已撤销
    :param token: 浏览器 Cookie 中的原始随机登录凭证
    """
    if token:
        get_db().auth_sessions.update_one({"token_hash": token_digest(token)}, {"$set": {"revoked_at": utcnow()}})
