"""连接 HTTP 任务与现有 LangGraph 工作流的适配层。"""

from pathlib import Path

from tool.logger import logger
from utils.sse_utils import SSEEvent, push_to_session
from utils.task_utils import (
    add_done_task, add_running_task, add_task_warning,
    get_task, set_task_result, update_task_status,
)


def _fail(task_id, message):
    update_task_status(task_id, "failed", error=message)
    push_to_session(task_id, SSEEvent.ERROR, {"error": message, "task_id": task_id})


def run_import_graph(task_id, file_path):
    try:
        update_task_status(task_id, "processing")
        add_running_task(task_id, "store_file")
        try:
            from config.minio_config import minio_config
            from utils.minio_utils import get_minio_client
            path = Path(file_path)
            get_minio_client().fput_object(
                minio_config.bucket_name, f"documents/{task_id}/{path.name}", str(path),
                content_type="application/pdf" if path.suffix.lower() == ".pdf" else "text/markdown",
            )
        except Exception:
            logger.exception("Object storage failed for task %s", task_id)
            add_task_warning(task_id, "原文件备份失败，已继续处理本地文件。")
        add_done_task(task_id, "store_file")
        from processor.import_processor.main_graph import KBImportWorkflow
        result = KBImportWorkflow().run({
            "task_id": task_id, "import_file_path": str(file_path),
            "file_dir": str(Path(file_path).parent),
        })
        for key, value in {
            "chunk_count": len(result.get("chunks") or []),
            "item_name": result.get("item_name"), "file_title": result.get("file_title"),
        }.items():
            set_task_result(task_id, key, value)
        update_task_status(task_id, "completed")
    except Exception:
        logger.exception("Import failed: %s", task_id)
        task = get_task(task_id)
        node = (task.get("running_list") or [""])[-1]
        step = {
            "node_entry": "文件读取", "node_pdf_to_md": "PDF 解析", "node_md_img": "图片处理",
            "node_document_split": "文档切片", "node_item_name_recognition": "主体识别",
            "node_bge_embedding": "向量化", "node_import_milvus": "知识入库",
        }.get(node, "文档处理")
        _fail(task_id, f"导入失败（{step}），请检查服务连接与文件内容后重试。")


def run_query_graph(task_id, session_id, query, is_stream):
    try:
        update_task_status(task_id, "processing")
        from processor.query_processor.main_graph import KBQueryWorkflow
        # invoke 始终执行完整工作流；答案增量由答案输出节点内部推送。
        result = KBQueryWorkflow().run({
            "task_id": task_id, "session_id": session_id, "original_query": query,
            "is_stream": is_stream, "embedding_chunks": [], "hyde_embedding_chunks": [],
            "web_search_docs": [], "rrf_chunks": [], "reranked_docs": [],
        }, stream=False)
        if not result.get("answer"):
            raise RuntimeError("Empty answer")
        for key in ("answer", "image_urls", "sources"):
            set_task_result(task_id, key, result.get(key, [] if key != "answer" else ""))
        update_task_status(task_id, "completed")
        task = get_task(task_id)
        push_to_session(task_id, SSEEvent.FINAL, {
            **task["result"], "status": "completed", "task_id": task_id,
            "done_list": task["done_list"], "warnings": task["warnings"],
        })
    except Exception:
        logger.exception("Query failed: %s", task_id)
        _fail(task_id, "问答处理失败，请检查数据库、模型服务连接后重试。")
