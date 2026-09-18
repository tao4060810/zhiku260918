"""单进程本地 Web 应用，可通过 python -m web.app 启动。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
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
    # 1. 服务启动时创建线程池，供文档导入和知识问答共用
    # 一次执行一个后台任务，其余任务排队，降低模型推理的显存和内存峰值。
    # 同名文档的导入也依次执行，避免删除旧切片和插入新切片时相互干扰。
    # 问答可能需要等待导入完成；此设置不限制整个 FastAPI 服务的线程数。
    app.state.executor = ThreadPoolExecutor(
        max_workers=1,  # 此线程池最多使用一个工作线程
        thread_name_prefix="zhiku"  # 工作线程名称前缀，便于调试时识别
    )

    # 2. 初始化完成，将控制权交给 FastAPI；服务关闭时继续执行下面的清理代码
    yield

    # 3. 服务关闭时等待已提交的任务执行完毕，再释放线程池资源
    app.state.executor.shutdown(
        wait=True,  # 等待线程池中的任务执行完毕
        cancel_futures=False  # 保留尚未开始的排队任务，让它们继续执行
    )


app = FastAPI(
    title="掌柜智库",  # 应用名称，显示在自动生成的 API 文档中。
    description="文档导入、知识检索与会话管理",  # API 文档中的应用说明，支持 Markdown。
    version="0.2.0",  # 应用 API 的版本标记，不是 FastAPI 框架版本，也不会自动实现接口版本控制。
    lifespan=lifespan,  # 传入生命周期函数，由 FastAPI 在启动和关闭时管理线程池资源。
)
# 将导入和查询接口注册到应用
app.include_router(import_router)
app.include_router(query_router)
# 注册静态文件地址，让浏览器能够通过 /static 地址访问项目中的静态文件
app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/chat.html")


@app.get("/chat.html", include_in_schema=False)
def chat_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="chat.html", context={"view": "chat", "page_title": "知识问答"}
    )


@app.get("/import.html", include_in_schema=False)
def import_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="import.html", context={"view": "import", "page_title": "文档管理"}
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.app:app", host="127.0.0.1", port=8000)
