"""后台固定使用任务记录中的身份，退出不会改变结果归属。"""
from pathlib import Path
import shutil

from tool.logger import logger
from utils.knowledge_access import require_kb_permission
from utils.knowledge_store import store_asset, finish_document, clean_failed_assets, mutation_lock
from utils.task_utils import add_done_task, add_running_task, get_task, finish_task, update_task_status, is_task_canceled, TaskCanceled


def run_import_graph(task_id, file_path):
    """
    按任务记录中的身份执行私有文档导入，发布新版本并清理产物
    :param task_id: 后台任务 ID
    :param file_path: 本次任务保存的本地原文件路径
    """
    # 1. 固定使用任务创建时保存的身份，排队期间退出或换账号不会改变归属
    task = get_task(task_id)
    try:
        # 尚未开始执行的任务也走统一清理分支，释放此前上传预留的文档和本地文件。
        if is_task_canceled(task_id):
            raise TaskCanceled()
        require_kb_permission(task["user_id"], task["kb_id"], "upload")
        update_task_status(task_id, "processing")
        add_running_task(task_id, "store_file")
        # 私有文件存储不可用时失败，不降级为公开链接。
        store_asset(task["user_id"], task["kb_id"], task["document_id"], task_id, file_path, "original")
        # 备份调用返回后复核取消；对象若已写入，由异常分支按本次任务范围清理。
        if is_task_canceled(task_id):
            raise TaskCanceled()
        add_done_task(task_id, "store_file")
        # 2. 执行导入流水线，将身份和文档范围传给各处理节点
        from processor.import_processor.main_graph import KBImportWorkflow
        result = KBImportWorkflow().graph.invoke({
            "task_id": task_id, "user_id": task["user_id"], "kb_id": task["kb_id"],
            "document_id": task["document_id"], "import_file_path": str(file_path),
            "file_dir": str(Path(file_path).parent),
        })
        # 取消与发布共用一把锁：取消成功的任务不能再成为可检索版本。
        with mutation_lock:
            if is_task_canceled(task_id):
                raise TaskCanceled()
            require_kb_permission(task["user_id"], task["kb_id"], "upload")
            finish_document(task, True)
            finish_task(task_id, result={"chunk_count": len(result.get("chunks") or []),
                        "item_name": result.get("item_name"), "file_title": result.get("file_title")})
        # 3. 旧版本清理失败不撤销已发布的新版本
        try:
            from utils.milvus_utils import cleanup_import_vectors
            cleanup_import_vectors(task, keep_current=True)
        except Exception:
            logger.warning("旧版本向量清理待重试: %s", task_id)
    except Exception:
        # 失败或取消均释放预留并清理本任务产物，不删除旧的已发布版本。
        if not is_task_canceled(task_id):
            logger.error("导入任务失败: %s", task_id)
        try:
            finish_document(task, False)
            clean_failed_assets(task)
            from utils.milvus_utils import cleanup_import_vectors
            cleanup_import_vectors(task)
        except Exception:
            logger.error("导入产物清理未完成: %s", task_id)
        finish_task(task_id, error="导入失败，请检查文件及数据库、解析和模型服务后重试。")

    finally:
        # 只清理符合本次知识库、文档和任务层级的目录，避免误删其他导入文件。
        folder = Path(file_path).resolve().parent
        if folder.name == task_id and folder.parent.name == task["document_id"] and folder.parent.parent.name == task["kb_id"]:
            shutil.rmtree(folder, ignore_errors=True)


def run_query_graph(task_id, session_id, query, is_stream):
    """
    按任务记录中的身份执行检索和回答生成，保存结果并发送结束事件
    :param task_id: 后台任务 ID
    :param session_id: 聊天会话 ID
    :param query: 用户提交的问题文本
    :param is_stream: 是否逐段推送回答，不控制 LangGraph 的执行方式
    """
    # 1. 开始执行前重新校验任务拥有者是否仍有知识库访问权限
    task = get_task(task_id)
    try:
        require_kb_permission(task["user_id"], task["kb_id"])
        update_task_status(task_id, "processing")
        from processor.query_processor.main_graph import KBQueryWorkflow
        # 2. LangGraph 执行完整流程；is_stream 只控制答案节点是否逐段发布文本
        result = KBQueryWorkflow().run({
            "task_id": task_id, "user_id": task["user_id"], "kb_id": task["kb_id"],
            "session_id": session_id, "original_query": query, "is_stream": is_stream,
            "embedding_chunks": [], "hyde_embedding_chunks": [], "web_search_docs": [],
            "rrf_chunks": [], "reranked_docs": [],
        }, stream=False)
        # 3. 返回结果前再次校验权限，统一保存答案并发布结束事件
        require_kb_permission(task["user_id"], task["kb_id"])
        if not result.get("answer"):
            raise RuntimeError("Empty answer")
        finish_task(task_id, result={key: result.get(key, [] if key != "answer" else "")
                                    for key in ("answer", "sources", "image_urls")})
    except Exception:
        logger.error("问答任务失败: %s", task_id)
        finish_task(task_id, error="问答处理失败，请检查数据库、模型服务连接后重试。")
