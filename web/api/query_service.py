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
from utils.task_utils import create_task, get_task, latest_session_task
from web.api.workflows import run_query_graph

router = APIRouter(tags=["知识问答"])
# 限制会话 ID 的长度和字符范围，供请求模型重复使用。
SessionId = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]


class QueryRequest(BaseModel):
    """
    问答请求模型：统一校验问题内容、会话 ID 和流式输出选项
    """
    query: str = Field(min_length=1, max_length=4000)
    session_id: SessionId | None = None  # 未传会话 ID 时由接口创建新会话。
    is_stream: bool = True  # 是否逐段推送回答；两种模式都会执行完整工作流。

    @field_validator("query")
    @classmethod
    def nonempty_query(cls, value):
        """
        去掉问题首尾空白，并拒绝仅包含空格或换行的输入
        :param value: 待校验的问题文本
        :return: 去掉首尾空白后的问题文本
        """
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空。")
        return value


@router.post("/query")
async def query(body: QueryRequest, request: Request):
    """
    提交用户问题，并根据流式选项返回任务标识或完整答案
    :param body: 已校验的问题内容、会话 ID 和流式输出选项
    :param request: 请求对象，用于获取应用共享的后台线程池
    :return: 流式模式返回会话和任务 ID，非流式模式返回完整结果
    """
    # 1. 创建独立任务；同一会话已有任务或队列已满时返回冲突提示。
    session_id = body.session_id or str(uuid4())
    try:
        task_id = create_task("query", session_id=session_id, query=body.query)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    # 2. 在线程池中执行工作流，避免模型调用和检索阻塞 HTTP 事件循环。
    future = request.app.state.executor.submit(run_query_graph, task_id, session_id, body.query, body.is_stream)
    # 3. 流式模式立即返回任务标识，由前端另行订阅 SSE
    if body.is_stream:
        # 前端收到任务 ID 后，通过 /stream 订阅后续进度和回答。
        return {"message": "问题已接收", "session_id": session_id, "task_id": task_id}
    # 4. 非流式模式等待工作流执行完毕，再读取完整答案
    # wrap_future 将线程池的 Future 转成可 await 的对象，等待时不阻塞事件循环。
    await asyncio.wrap_future(future)
    task = get_task(task_id)
    if task["status"] == "failed":
        raise HTTPException(503, task["error"])
    return {"session_id": session_id, "task_id": task_id, **task["result"], "done_list": task["done_list"], "warnings": task["warnings"]}


@router.get("/stream/{session_id}")
async def stream(session_id: str, request: Request, task_id: str | None = None):
    """
    订阅问答任务的 SSE 事件，持续发送进度和回答
    :param session_id: 任务所属的会话 ID
    :param request: 请求对象，用于读取重连编号和检测客户端断开
    :param task_id: 要订阅的任务 ID，未传时使用该会话最近的任务
    :return: 包含任务事件的 SSE 流式响应
    """
    # 1. 获取任务并校验所属会话，避免将其他会话的任务作为订阅目标
    task = get_task(task_id) if task_id else latest_session_task(session_id)
    if not task or task["session_id"] != session_id:
        raise HTTPException(404, "问答任务不存在或已过期。")
    # 2. 读取重连时携带的事件编号，从该位置之后继续发送
    try:
        cursor = max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(400, "无效的事件编号。") from None
    # 3. 返回流式响应；响应头要求不使用缓存，并提示 Nginx 关闭响应缓冲
    return StreamingResponse(
        sse_generator(task["task_id"], request, cursor), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions")
def sessions(limit: int = Query(50, ge=1, le=100)):
    """
    查询最近会话，供页面侧栏展示
    :param limit: 最多返回的会话数量，默认 50，允许 1 至 100
    :return: 包含最近会话列表的字典
    """
    try:
        return {"items": history_store.list_sessions(limit)}
    except Exception:
        raise HTTPException(503, "会话数据库暂不可用。") from None


@router.get("/history/{session_id}")
def history(session_id: str, limit: int = Query(100, ge=1, le=500)):
    """
    查询会话历史，并提供最近任务以便前端恢复未完成的问答
    :param session_id: 要查询的会话 ID
    :param limit: 最多读取的消息数量，默认 100，允许 1 至 500
    :return: 会话 ID、历史消息列表和最近任务信息
    """
    try:
        # 1. 从 MongoDB 读取最近消息
        records = history_store.get_recent_messages(session_id, limit)
        # 2. 转换旧版引用格式和 MongoDB 标识，仅调整输出，不回写历史原文
        items = [{**present_history_message(r), "_id": str(r.get("_id", ""))} for r in records]
        # 3. 附带最近任务，前端根据其状态判断是否需要继续接收回答
        return {"session_id": session_id, "items": items, "active_task": latest_session_task(session_id)}
    except Exception:
        raise HTTPException(503, "聊天记录暂时无法加载，请稍后重试。") from None


@router.delete("/history/{session_id}")
def clear_chat_history(session_id: str):
    """
    删除指定会话的聊天记录
    :param session_id: 要删除聊天记录的会话 ID
    :return: 删除提示和实际删除的消息数量
    """
    # 1. 拒绝删除仍在处理的会话，避免随后生成的答案又写入历史
    task = latest_session_task(session_id)
    if task and task["status"] not in {"completed", "failed"}:
        raise HTTPException(409, "当前会话仍在处理中，暂时无法删除。")
    try:
        # 2. 删除 MongoDB 中的会话消息，返回删除数量
        count = history_store.clear_history(session_id)
        return {"message": "历史会话已清空", "deleted_count": count}
    except Exception:
        raise HTTPException(503, "删除失败，请稍后重试。") from None
