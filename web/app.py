"""单进程本地 Web 应用，可通过 python -m web.app 启动。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi import Depends
from pymongo.errors import PyMongoError
from utils.auth_utils import LoginRequired
from utils.knowledge_access import AccessDenied
from utils.rate_limit_utils import RateLimited
from config.auth_config import auth_config
from web.api.auth_dependencies import CurrentUser, get_current_user
from web.api.auth_service import router as auth_router
from web.api.knowledge_service import router as knowledge_router
from web.api.asset_service import router as asset_router
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from web.api.import_service import router as import_router
from web.api.query_service import router as query_router

ROOT = Path(__file__).resolve().parents[1]
load_dotenv()

# 两个业务页面继承同一基础模板，复用导航和会话列表。
templates = Jinja2Templates(directory=ROOT / "web" / "templates")

@asynccontextmanager
async def lifespan(app):
    """
    应用生命周期：启动时创建后台线程池，关闭时释放线程池资源
    :param app: FastAPI 应用实例，通过 app.state 共享线程池
    """
    # 1. 恢复上次中断的导入预留；数据库暂不可用时由后续认证请求补做
    from utils.knowledge_store import recover_interrupted_imports
    app.state.imports_recovered = False
    try:
        await run_in_threadpool(recover_interrupted_imports)
        app.state.imports_recovered = True
    except PyMongoError:
        pass  # 保持认证错误为 503；后续请求仍不可匿名放行。
    # 2. 创建导入和问答共用的后台线程池
    # 一次执行一个后台任务，其余任务排队，降低模型推理的显存和内存峰值。
    # 同名文档的导入也依次执行，避免版本写入和清理时相互干扰。
    # 问答可能需要等待导入完成；此设置不限制整个 FastAPI 服务的线程数。
    app.state.executor = ThreadPoolExecutor(
        max_workers=1,  # 此线程池最多使用一个工作线程
        thread_name_prefix="zhiku"  # 工作线程名称前缀，便于调试时识别
    )

    # 3. 初始化完成，将控制权交给 FastAPI；服务关闭时继续执行下面的清理代码
    yield

    # 4. 服务关闭时等待已提交的任务执行完毕，再释放线程池资源
    app.state.executor.shutdown(
        wait=True,  # 等待线程池中的任务执行完毕
        cancel_futures=False  # 保留尚未开始的排队任务，让它们继续执行
    )


app = FastAPI(
    # 生产环境关闭交互式文档和接口结构地址，开发环境保留调试入口。
    docs_url=None if auth_config.production else "/docs",
    redoc_url=None if auth_config.production else "/redoc",
    openapi_url=None if auth_config.production else "/openapi.json",
    title="掌柜智库",  # 应用名称，显示在自动生成的 API 文档中。
    description="文档导入、知识检索与会话管理",  # API 文档中的应用说明，支持 Markdown。
    version="0.2.0",  # 应用 API 的版本标记，不是 FastAPI 框架版本，也不会自动实现接口版本控制。
    lifespan=lifespan,  # 传入生命周期函数，由 FastAPI 在启动和关闭时管理线程池资源。
)
# 注册文档、问答、账号、知识库及私有文件接口。
app.include_router(import_router)
app.include_router(query_router)
app.include_router(auth_router)
app.include_router(knowledge_router)
app.include_router(asset_router)
# 注册静态文件地址，让浏览器能够通过 /static 地址访问项目中的静态文件
app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/chat.html")


@app.get("/chat.html", include_in_schema=False)
def chat_page(request: Request, user: CurrentUser):
    """
    为已登录用户渲染知识问答页
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param user: 通过认证依赖取得的当前用户记录
    :return: 聊天页面的模板响应
    """
    return templates.TemplateResponse(
        request=request, name="chat.html", context={"view": "chat", "page_title": "知识问答", "user": user}
    )


@app.get("/import.html", include_in_schema=False)
def import_page(request: Request, user: CurrentUser):
    """
    为已登录用户渲染文档管理页
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param user: 通过认证依赖取得的当前用户记录
    :return: 文档管理页面的模板响应
    """
    return templates.TemplateResponse(
        request=request, name="import.html", context={"view": "import", "page_title": "文档管理", "user": user}
    )


@app.exception_handler(LoginRequired)
async def login_required(request, exc):
    """
    处理登录失效：页面跳转登录页，接口返回 401
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param exc: 登录失效异常
    :return: 登录重定向或认证错误响应
    """
    if request.url.path.endswith(".html"):
        return RedirectResponse("/login.html", status_code=303, headers={"Cache-Control": "no-store"})
    return JSONResponse({"detail": "登录已失效，请重新登录。", "code": "AUTH_EXPIRED"}, status_code=401)


@app.exception_handler(AccessDenied)
async def access_denied(request, exc):
    """
    统一返回资源不可访问提示，避免暴露其他账号资源是否存在
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param exc: 知识库或会话权限异常
    :return: HTTP 404 错误响应
    """
    return JSONResponse({"detail": "资源不存在或无权访问。"}, status_code=404)


@app.exception_handler(PyMongoError)
async def database_error(request, exc):
    """
    将数据库异常转换为前端可展示的服务不可用提示
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param exc: MongoDB 访问异常
    :return: HTTP 503 错误响应
    """
    return JSONResponse({"detail": "数据库暂不可用，请稍后重试。"}, status_code=503)


@app.exception_handler(RateLimited)
async def rate_limited(request, exc):
    """
    将限流异常转换为带等待时长的响应
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param exc: 包含剩余等待秒数的限流异常
    :return: HTTP 429 响应及 Retry-After 请求重试提示
    """
    return JSONResponse({"detail": "请求过于频繁，请稍后重试。"}, status_code=429,
                        headers={"Retry-After": str(exc.retry_after)})


@app.middleware("http")
async def private_cache(request, call_next):
    """
    禁止缓存私有业务响应，并关闭浏览器对内容类型的猜测
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param call_next: 调用后续中间件及接口处理函数
    :return: 附带响应头的原业务响应
    """
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/login.html", include_in_schema=False)
def login_page(request: Request):
    """
    渲染登录页，根据配置决定是否展示注册入口
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :return: 登录页面模板响应
    """
    return templates.TemplateResponse(request=request, name="login.html",
                                      context={"registration": auth_config.registration})


@app.get("/register.html", include_in_schema=False)
def register_page(request: Request):
    """
    渲染注册页，注册关闭时展示相应提示
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :return: 注册页面模板响应
    """
    return templates.TemplateResponse(request=request, name="register.html",
                                      context={"registration": auth_config.registration})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.app:app", host="127.0.0.1", port=8000)
