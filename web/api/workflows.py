"""连接 HTTP 任务与现有 LangGraph 工作流的适配层。"""

from pathlib import Path
from tool.logger import logger
from utils.sse_utils import SSEEvent, push_to_session
from utils.task_utils import (
    add_done_task, add_running_task, add_task_warning,
    get_task, set_task_result, update_task_status,
)


def _fail(task_id, message):
    """
    统一记录任务失败状态，并发布错误事件
    :param task_id: 失败任务的 ID
    :param message: 提供给前端展示的错误说明
    """
    # 1. 保存失败状态，供任务轮询接口读取
    update_task_status(task_id, "failed", error=message)
    # 2. 保存错误事件，供 SSE 连接读取
    push_to_session(task_id, SSEEvent.ERROR, {"error": message, "task_id": task_id})


def run_import_graph(task_id, file_path):
    """
    后台执行文档备份和导入工作流，将进度与结果写入任务记录
    :param task_id: 导入任务的 ID
    :param file_path: 已保存到本地的上传文件路径
    """
    try:
        # 1. 更新任务状态，开始备份原始文件
        update_task_status(task_id, "processing")
        add_running_task(task_id, "store_file")
        try:
            # 任务执行时再加载服务配置，将原文件备份到 MinIO 的任务专属路径。
            from config.minio_config import minio_config
            from utils.minio_utils import get_minio_client
            path = Path(file_path)
            get_minio_client().fput_object(
                minio_config.bucket_name, f"documents/{task_id}/{path.name}", str(path),
                content_type="application/pdf" if path.suffix.lower() == ".pdf" else "text/markdown",
            )
        except Exception:
            # 备份失败只记录警告，后续仍可使用本地文件继续解析和入库。
            logger.exception("Object storage failed for task %s", task_id)
            add_task_warning(task_id, "原文件备份失败，已继续处理本地文件。")
        add_done_task(task_id, "store_file")
        # 2. 执行导入工作流，节点通过任务 ID 更新处理进度
        from processor.import_processor.main_graph import KBImportWorkflow
        result = KBImportWorkflow().run({
            "task_id": task_id, "import_file_path": str(file_path),
            "file_dir": str(Path(file_path).parent),
        })
        # 3. 保存切片数量、主体名称和文件标题，标记导入完成
        for key, value in {
            "chunk_count": len(result.get("chunks") or []),
            "item_name": result.get("item_name"), "file_title": result.get("file_title"),
        }.items():
            set_task_result(task_id, key, value)
        update_task_status(task_id, "completed")
    except Exception:
        # 日志保留完整异常，界面使用最后运行节点对应的中文步骤提示失败位置。
        logger.exception("Import failed: %s", task_id)
        task = get_task(task_id)
        node = (task.get("running_list") or [""])[-1]
        step = {
            "node_entry": "文件读取",
            "node_pdf_to_md": "PDF 解析",
            "node_md_img": "图片处理",
            "node_document_split": "文档切片",
            "node_item_name_recognition": "主体识别",
            "node_bge_embedding": "向量化",
            "node_import_milvus": "知识入库",
        }.get(node, "文档处理")
        _fail(task_id, f"导入失败（{step}），请检查服务连接与文件内容后重试。")


def run_query_graph(task_id, session_id, query, is_stream):
    """
    后台执行知识检索和回答生成，保存结果并发布结束事件
    :param task_id: 问答任务的 ID，用于记录进度和结果
    :param session_id: 会话 ID，用于关联聊天历史
    :param query: 用户提交的问题文本
    :param is_stream: 是否逐段推送生成的回答
    """
    try:
        # 1. 更新任务状态并加载问答工作流
        update_task_status(task_id, "processing")
        from processor.query_processor.main_graph import KBQueryWorkflow
        # 2. 构建初始状态，执行检索、重排和回答生成
        # stream=False 控制 LangGraph 使用 invoke 执行完整工作流；
        # 状态中的 is_stream 控制回答是否逐段推送，两者用途不同。
        result = KBQueryWorkflow().run({
            "task_id": task_id, "session_id": session_id, "original_query": query,
            "is_stream": is_stream, "embedding_chunks": [], "hyde_embedding_chunks": [],
            "web_search_docs": [], "rrf_chunks": [], "reranked_docs": [],
        }, stream=False)
        if not result.get("answer"):
            # 没有答案时按任务失败处理，避免前端将空结果显示为成功。
            raise RuntimeError("Empty answer")
        # 3. 保存完整回答、图片和引用，标记任务完成
        for key in ("answer", "image_urls", "sources"):
            set_task_result(task_id, key, result.get(key, [] if key != "answer" else ""))
        update_task_status(task_id, "completed")
        task = get_task(task_id)
        # 4. 发布 FINAL 事件，通知前端结束等待并展示完整结果
        push_to_session(task_id, SSEEvent.FINAL, {
            **task["result"], "status": "completed", "task_id": task_id,
            "done_list": task["done_list"], "warnings": task["warnings"],
        })
    except Exception:
        # 后台线程无法直接返回 HTTP 错误，因此通过任务状态和事件通知前端。
        logger.exception("Query failed: %s", task_id)
        _fail(task_id, "问答处理失败，请检查数据库、模型服务连接后重试。")
