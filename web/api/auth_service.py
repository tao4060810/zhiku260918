"""基础账号路由，不接受客户端指定用户 ID 或角色。"""
import re

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from config.auth_config import auth_config
from utils import auth_utils
from utils.auth_utils import LoginRequired
from utils.knowledge_store import ensure_default_kb
from utils.rate_limit_utils import check_rate
from utils.user_store import create_user, get_db, public_user, utcnow
from web.api.auth_dependencies import CurrentUser, require_origin

router = APIRouter(prefix="/auth", tags=["账号"])


class LoginBody(BaseModel):
    """登录请求：只接收用户名和密码，拒绝客户端额外指定身份字段。"""
    model_config = ConfigDict(extra="forbid")
    username: str = Field(max_length=64)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize(cls, value):
        """
        统一用户名大小写并校验格式
        :param value: 请求中的用户名
        :return: 去掉首尾空白并转为小写的用户名
        """
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{3,32}", value):
            raise ValueError("用户名为 3–32 位字母、数字、下划线或连字符")
        return value


class RegisterBody(LoginBody):
    """注册请求：复用用户名校验，并增加密码长度和二次确认。"""
    password: str = Field(min_length=12, max_length=128)
    password_confirm: str = Field(min_length=12, max_length=128)

    @model_validator(mode="after")
    def match(self):
        """
        校验注册时两次输入的密码是否一致
        :return: 通过校验的注册请求对象
        """
        if self.password != self.password_confirm:
            raise ValueError("两次密码不一致")
        return self


class PasswordBody(BaseModel):
    """修改密码请求：包含旧密码、新密码和确认值，不接收用户 ID。"""
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)
    password_confirm: str = Field(min_length=12, max_length=128)

    @model_validator(mode="after")
    def match(self):
        """
        校验修改密码时两次输入的新密码是否一致
        :return: 通过校验的修改密码请求对象
        """
        if self.new_password != self.password_confirm:
            raise ValueError("两次密码不一致")
        return self


def check_public_request(request):
    """
    检查未登录写请求的来源和 JSON 内容类型
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    """
    require_origin(request)
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "请使用 JSON 请求。")


@router.post("/register", status_code=201)
def register(body: RegisterBody, request: Request):
    """
    注册普通用户，并创建其默认私有知识库
    :param body: 经过用户名与密码一致性校验的注册请求
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :return: 新账号的公开信息，不包含密码摘要
    """
    # 1. 检查请求来源、注册开关及当前 IP 的注册频率
    check_public_request(request)
    if not auth_config.registration:
        raise HTTPException(403, "当前未开放注册。")
    check_rate(("register", request.client.host if request.client else "unknown"), 5, 3600)
    # 2. 保存密码摘要并创建普通用户，唯一索引负责拒绝重复用户名
    try:
        user = create_user(body.username, auth_utils.passwords.hash(body.password))
    except DuplicateKeyError:
        raise HTTPException(409, "用户名已被使用。") from None
    return {"user": public_user(user)}


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    """
    校验账号密码并签发登录 Cookie
    :param body: 经过格式校验的登录请求
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param response: 响应对象，用于写入或删除登录 Cookie
    :return: 用户公开信息及后续写请求使用的 CSRF 令牌
    """
    # 1. 检查请求来源，分别限制 IP 和用户名维度的登录尝试
    check_public_request(request)
    check_rate(("login-ip", request.client.host if request.client else "unknown"), 20, 60)
    check_rate(("login-name", body.username), 10, 900)
    user = get_db().users.find_one({"username": body.username})
    # 2. 用户不存在时仍执行密码校验，并统一错误提示，减少账号存在性差异
    valid = auth_utils.passwords.verify(body.password, user["password_hash"] if user else auth_utils.DUMMY_HASH)
    if not valid or not user or not user["is_active"]:
        raise HTTPException(401, "用户名或密码错误。")
    # 3. 准备默认知识库，撤销浏览器旧凭证并签发新凭证
    ensure_default_kb(user["_id"])
    auth_utils.revoke_session(request.cookies.get(auth_config.cookie))
    token, record = auth_utils.issue_session(user)
    # 4. 登录凭证放入 HttpOnly Cookie；CSRF 令牌单独返回给前端写请求使用
    response.set_cookie(auth_config.cookie, token, max_age=auth_config.ttl,
                        httponly=True, secure=auth_config.secure, samesite="lax", path="/")
    return {"user": public_user(user), "csrf_token": record["csrf_token"]}


@router.get("/me")
def me(request: Request, user: CurrentUser):
    """
    读取当前登录用户及默认知识库
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param user: 通过认证依赖取得的当前用户记录
    :return: 用户公开信息、CSRF 令牌和默认知识库 ID
    """
    kb = ensure_default_kb(user["_id"])
    return {"user": public_user(user), "csrf_token": request.state.login["csrf_token"], "kb_id": kb["_id"]}


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response):
    """
    撤销当前登录凭证，并清除浏览器中的登录 Cookie
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param response: 响应对象，用于写入或删除登录 Cookie
    """
    require_origin(request)
    from web.api.auth_dependencies import get_current_user
    try:
        get_current_user(request)
    except LoginRequired:
        # 已失效的登录也允许清除 Cookie；有效登录仍需通过写请求校验。
        pass
    auth_utils.revoke_session(request.cookies.get(auth_config.cookie))
    response.delete_cookie(auth_config.cookie, path="/", secure=auth_config.secure, httponly=True, samesite="lax")


@router.post("/change-password", status_code=204)
def change_password(body: PasswordBody, request: Request, response: Response, user: CurrentUser):
    """
    校验旧密码并更新密码摘要，使该账号已有登录凭证失效
    :param body: 当前密码、新密码和新密码确认值
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param response: 响应对象，用于写入或删除登录 Cookie
    :param user: 通过认证依赖取得的当前用户记录
    """
    # 1. 限制修改频率并确认当前密码
    check_rate(("password", user["_id"]), 5, 900)
    if not auth_utils.passwords.verify(body.current_password, user["password_hash"]):
        raise HTTPException(400, "当前密码错误。")
    # 2. 按认证版本更新密码并递增版本，旧设备的登录记录将无法通过后续校验
    result = get_db().users.update_one({"_id": user["_id"], "auth_version": user["auth_version"]}, {
        "$set": {"password_hash": auth_utils.passwords.hash(body.new_password), "updated_at": utcnow()},
        "$inc": {"auth_version": 1},
    })
    if not result.matched_count:
        raise LoginRequired()
    response.delete_cookie(auth_config.cookie, path="/", secure=auth_config.secure, httponly=True, samesite="lax")
