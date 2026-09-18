"""单进程本地 Web 应用，可通过 python -m web.app 启动。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from web.api.import_service import router as import_router
from web.api.query_service import router as query_router


@asynccontextmanager
async def lifespan(app):
    # 每次只运行一条流水线，避免并发加载 GPU 模型及重复导入同名文件。
    app.state.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="zhiku")
    yield
    app.state.executor.shutdown(wait=True, cancel_futures=False)


app = FastAPI(title="掌柜智库", description="文档导入、知识检索与会话管理", version="0.2.0", lifespan=lifespan)
app.include_router(import_router)
app.include_router(query_router)
app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/chat.html")


@app.get("/chat.html", include_in_schema=False)
@app.get("/import.html", include_in_schema=False)
def page():
    return FileResponse(ROOT / "web" / "page" / "index.html")


@app.get("/health", tags=["服务状态"])
def health():
    return {"ok": True}


@app.get("/health/services", tags=["服务状态"])
def services():
    import socket
    import os
    from urllib.parse import urlsplit

    results = {}
    for name, env, port in [("MongoDB", "MONGO_URL", 27017), ("Milvus", "MILVUS_URL", 19530), ("MinIO", "MINIO_ENDPOINT", 9000)]:
        value = os.getenv(env, "")
        url = urlsplit(value if "://" in value else "//" + value)
        try:
            with socket.create_connection((url.hostname, url.port or port), timeout=1):
                results[name] = "reachable"
        except (OSError, ValueError, TypeError):
            results[name] = "unreachable"
    return {"ok": all(v == "reachable" for v in results.values()), "services": results, "check": "tcp"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.app:app", host="127.0.0.1", port=8000)
