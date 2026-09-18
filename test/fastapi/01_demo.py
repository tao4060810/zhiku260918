import uvicorn
from fastapi import FastAPI

app = FastAPI()

@app.get("/", summary="第一个测试")
async def index():
    return {"message": "Hello World!"}

if __name__ == "__main__":
    uvicorn.run(
        app=app,
        host="127.0.0.1",  # 仅本地访问，生产环境改为0.0.0.0（允许所有IP访问）
        port=8000  # 服务端口
    )