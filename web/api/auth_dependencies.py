"""框架原生依赖：读取当前用户；所有写请求显式校验 Origin 和 CSRF。"""
import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from config.auth_config import auth_config
from utils.auth_utils import authenticate


def require_origin(request: Request):
    """
    检查请求 Origin 是否位于配置的允许来源中
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    """
    if request.headers.get("origin") not in auth_config.origins:
        raise HTTPException(403, "请求来源不允许。")


def get_current_user(request: Request):
    """
    校验登录凭证，恢复中断预留，并检查写请求的来源与 CSRF 令牌
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :return: 当前有效用户记录，同时将登录记录存入 request.state
    """
    # 1. 身份只从登录 Cookie 对应的服务端记录取得，不相信客户端传入的用户 ID
    user, login = authenticate(request.cookies.get(auth_config.cookie))
    # 2. 启动时数据库不可用的情况下，在首次成功认证后补做中断预留恢复
    if not getattr(request.app.state, "imports_recovered", False):
        from utils.knowledge_store import mutation_lock, recover_interrupted_imports
        with mutation_lock:
            if not getattr(request.app.state, "imports_recovered", False):
                recover_interrupted_imports()
                request.app.state.imports_recovered = True
    # 3. 将认证结果存入本次请求上下文，写请求额外校验来源和 CSRF 令牌
    request.state.user = user
    request.state.login = login
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        require_origin(request)
        if not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), login["csrf_token"].encode()):
            raise HTTPException(403, "页面凭证已失效，请刷新后重试。")
    return user


# 路由声明 user: CurrentUser 即可复用上述认证过程，成功后获得当前用户记录。
CurrentUser = Annotated[dict, Depends(get_current_user)]
