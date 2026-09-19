"""所有聊天与任务访问先校验当前用户和知识库。"""
import asyncio
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from config.auth_config import auth_config
from utils import mongo_history_utils as history_store
from utils.answer_presentation import present_history_message
from utils.auth_utils import authenticate
from utils.knowledge_access import AccessDenied, require_chat, require_kb_permission
from utils.knowledge_store import ensure_default_kb, mutation_lock
from utils.sse_utils import sse_generator
from utils.task_utils import create_task, finish_task, get_task, latest_session_task, TERMINAL
from utils.user_store import get_db, utcnow
from web.api.auth_dependencies import CurrentUser
from web.api.workflows import run_query_graph

router = APIRouter(tags=["知识问答"])


class QueryRequest(BaseModel):
    """问答输入：知识库必须显式指定，用户身份由认证依赖确定。"""
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4000)
    kb_id: str = Field(min_length=36, max_length=36)
    session_id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    is_stream: bool = True

    @field_validator("query")
    @classmethod
    def nonempty_query(cls, value):
        """
        拒绝空白问题并去掉首尾空白
        :param value: 请求中的原始问题
        :return: 整理后的问题文本
        """
        if not value.strip():
            raise ValueError("问题不能为空")
        return value.strip()


def submit_query(body, request, user):
    """
    在共享锁内校验会话归属、创建任务并提交问答工作流
    :param body: 包含问题、知识库、可选会话和流式选项的请求
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param user: 通过认证依赖取得的当前用户记录
    :return: 会话 ID、任务 ID 和线程池 Future
    """
    # 1. 与会话删除、导入预留共用锁，确保权限校验和任务创建连续完成
    with mutation_lock:
        require_kb_permission(user["_id"], body.kb_id)
        # 2. 旧会话不能更换所属知识库；新会话 ID 统一由服务端生成
        if body.session_id:
            chat = require_chat(user["_id"], body.session_id)
            if chat["kb_id"] != body.kb_id:
                raise HTTPException(409, "更换知识库需要新建会话。")
            session_id = body.session_id
        else:
            session_id = str(uuid4())
            get_db().chat_sessions.insert_one({"_id": session_id, "user_id": user["_id"],
                                              "kb_id": body.kb_id, "created_at": utcnow()})
        # 3. 创建携带用户和知识库归属的任务，失败时撤销本次新建的空会话
        try:
            task_id = create_task("query", user_id=user["_id"], kb_id=body.kb_id,
                                  session_id=session_id, query=body.query)
        except ValueError as exc:
            if not body.session_id:
                get_db().chat_sessions.delete_one({"_id": session_id, "user_id": user["_id"], "kb_id": body.kb_id})
            raise HTTPException(409, str(exc)) from None
        # 4. 提交后台工作流；服务关闭导致提交失败时及时结束任务
        try:
            future = request.app.state.executor.submit(run_query_graph, task_id, session_id, body.query, body.is_stream)
        except RuntimeError:
            finish_task(task_id, error="服务正在关闭，请稍后重试。")
            raise HTTPException(503, "服务正在关闭，请稍后重试。") from None
        return session_id, task_id, future


@router.post("/query")
async def query(body: QueryRequest, request: Request, user: CurrentUser):
    """
    提交问题，按流式选项返回任务标识或等待完整回答
    :param body: 已校验的问答请求
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param user: 通过认证依赖取得的当前用户记录
    :return: 流式模式返回任务信息，非流式模式返回完整回答
    """
    # 1. 将同步数据库校验放到线程池执行，避免阻塞 HTTP 事件循环
    session_id, task_id, future = await run_in_threadpool(submit_query, body, request, user)
    if body.is_stream:
        return {"message": "问题已接收", "session_id": session_id, "task_id": task_id}
    # 2. 非流式模式等待后台完成，返回答案前再次检查登录与会话权限
    await asyncio.wrap_future(future)
    await run_in_threadpool(authenticate, request.cookies.get(auth_config.cookie))
    await run_in_threadpool(require_chat, user["_id"], session_id, body.kb_id)
    task = get_task(task_id)
    if task["status"] == "failed":
        raise HTTPException(503, task["error"])
    return {"session_id": session_id, "task_id": task_id, **task["result"],
            "done_list": task["done_list"], "warnings": task["warnings"]}


@router.get("/stream/{session_id}")
def stream(session_id: str, request: Request, user: CurrentUser, task_id: str | None = None):
    """
    校验会话和任务归属后建立 SSE 连接，并在连接期间重新鉴权
    :param session_id: 聊天会话 ID
    :param request: 当前 HTTP 请求，用于读取 Cookie、请求头或应用资源
    :param user: 通过认证依赖取得的当前用户记录
    :param task_id: 指定任务 ID，省略时使用该用户在会话中的最近任务
    :return: 持续输出任务事件的 SSE 响应
    """
    # 1. 同时匹配用户、会话、知识库和任务类型，防止混用其他范围的任务 ID
    chat = require_chat(user["_id"], session_id)
    task = get_task(task_id) if task_id else latest_session_task(session_id, user_id=user["_id"])
    if not task or task["kind"] != "query" or task["user_id"] != user["_id"] or task["session_id"] != session_id or task["kb_id"] != chat["kb_id"]:
        raise HTTPException(404, "问答任务不存在或已过期。")
    # 2. 读取浏览器重连编号，只继续发送后续事件
    try:
        cursor = max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(400, "无效的事件编号。") from None
    token = request.cookies.get(auth_config.cookie)

    def validate():
        """
        重新检查原登录凭证和会话权限，供 SSE 定期调用
        """
        current, _ = authenticate(token, touch=False)
        if current["_id"] != user["_id"]:
            raise AccessDenied()
        require_chat(user["_id"], session_id, chat["kb_id"])

    # 3. 把重新鉴权函数交给生成器，连接建立后也会检查注销和权限变化
    return StreamingResponse(sse_generator(task["task_id"], request, cursor, validate=validate),
        media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/sessions")
def sessions(user: CurrentUser, limit: int = Query(50, ge=1, le=100)):
    """
    读取当前用户默认知识库下的最近会话
    :param user: 通过认证依赖取得的当前用户记录
    :param limit: 最多返回的记录数量
    :return: 侧栏展示使用的会话摘要列表
    """
    kb = ensure_default_kb(user["_id"])
    return {"items": history_store.list_sessions(limit, user_id=user["_id"], kb_id=kb["_id"])}


@router.get("/history/{session_id}")
def history(session_id: str, user: CurrentUser, limit: int = Query(100, ge=1, le=500)):
    """
    读取当前用户的会话历史及最近任务，供页面恢复回答
    :param session_id: 聊天会话 ID
    :param user: 通过认证依赖取得的当前用户记录
    :param limit: 最多返回的记录数量
    :return: 历史消息列表、会话 ID 和最近任务
    """
    chat = require_chat(user["_id"], session_id)
    records = history_store.get_recent_messages(session_id, limit, user_id=user["_id"], kb_id=chat["kb_id"])
    items = [{**present_history_message(r), "_id": str(r["_id"])} for r in records]
    return {"session_id": session_id, "items": items,
            "active_task": latest_session_task(session_id, user_id=user["_id"])}


@router.delete("/history/{session_id}")
def clear_chat_history(session_id: str, user: CurrentUser):
    """
    检查任务状态后删除当前用户的会话记录
    :param session_id: 聊天会话 ID
    :param user: 通过认证依赖取得的当前用户记录
    :return: 删除提示及实际删除的消息数量
    """
    with mutation_lock:
        require_chat(user["_id"], session_id)
        task = latest_session_task(session_id, user_id=user["_id"])
        if task and task["status"] not in TERMINAL:
            raise HTTPException(409, "当前会话仍在处理中，暂时无法删除。")
        count = history_store.clear_history(session_id, user_id=user["_id"])
        return {"message": "历史会话已清空", "deleted_count": count}
