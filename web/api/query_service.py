# 知识问答接口：提交查询、订阅流式结果，以及查询和删除会话历史。
import asyncio
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from utils import mongo_history_utils as history_store
from utils.answer_presentation import present_history_message
from utils.sse_utils import sse_generator
from utils.task_utils import TaskConflict, create_task, get_task, latest_session_task
from web.api.workflows import run_query_graph

router = APIRouter(tags=["知识问答"])
SessionId = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]


class QueryRequest(BaseModel):
    # 统一校验问题长度和会话标识；未传会话 ID 时由接口创建新会话。
    query: str = Field(min_length=1, max_length=4000)
    session_id: SessionId | None = None
    is_stream: bool = True

    @field_validator("query")
    @classmethod
    def nonempty_query(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空。")
        return value


@router.post("/query")
async def query(body: QueryRequest, request: Request):
    # 1. 创建独立任务；同一会话已有任务或队列已满时返回冲突提示。
    session_id = body.session_id or str(uuid4())
    try:
        task_id = create_task("query", session_id=session_id, query=body.query)
    except TaskConflict as exc:
        raise HTTPException(409, str(exc)) from None
    # 2. 在线程池中执行工作流，避免模型调用和检索阻塞 HTTP 事件循环。
    future = request.app.state.executor.submit(run_query_graph, task_id, session_id, body.query, body.is_stream)
    if body.is_stream:
        return {"message": "问题已接收", "session_id": session_id, "task_id": task_id}
    # 3. 流式请求立即返回任务标识；非流式请求异步等待完整答案。
    await asyncio.wrap_future(future)
    task = get_task(task_id)
    if task["status"] == "failed":
        raise HTTPException(503, task["error"])
    return {"session_id": session_id, "task_id": task_id, **task["result"], "done_list": task["done_list"], "warnings": task["warnings"]}


@router.get("/stream/{session_id}")
async def stream(session_id: str, request: Request, task_id: str | None = None):
    # 校验任务所属会话，避免订阅其他会话的回答；未指定任务时取本会话最新任务。
    task = get_task(task_id) if task_id else latest_session_task(session_id)
    if not task or task["session_id"] != session_id:
        raise HTTPException(404, "问答任务不存在或已过期。")
    # 浏览器重连时携带最后收到的事件编号，从该位置继续重放后续事件。
    try:
        cursor = max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(400, "无效的事件编号。") from None
    return StreamingResponse(
        sse_generator(task["task_id"], request, cursor), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions")
def sessions(limit: int = Query(50, ge=1, le=100)):
    try:
        return {"items": history_store.list_sessions(limit)}
    except Exception:
        raise HTTPException(503, "会话数据库暂不可用。") from None


@router.get("/history/{session_id}")
def history(session_id: str, limit: int = Query(100, ge=1, le=500)):
    try:
        records = history_store.get_recent_messages(session_id, limit)
        # 兼容旧版引用格式并转换 MongoDB 标识，仅调整接口输出，不回写历史原文。
        items = [{**present_history_message(r), "_id": str(r.get("_id", ""))} for r in records]
        return {"session_id": session_id, "items": items, "active_task": latest_session_task(session_id)}
    except Exception:
        raise HTTPException(503, "聊天记录暂时无法加载，请稍后重试。") from None


@router.delete("/history/{session_id}")
def clear_chat_history(session_id: str):
    # 删除前检查任务状态，拒绝删除仍在处理的会话，避免随后生成的答案又写入历史。
    task = latest_session_task(session_id)
    if task and task["status"] not in {"completed", "failed"}:
        raise HTTPException(409, "当前会话仍在处理中，暂时无法删除。")
    try:
        count = history_store.clear_history(session_id)
        return {"message": "历史会话已清空", "deleted_count": count}
    except Exception:
        raise HTTPException(503, "删除失败，请稍后重试。") from None
