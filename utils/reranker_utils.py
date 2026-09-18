# utils/reranker_http_utils.py

import os

from dotenv import load_dotenv

from config.reranker_config import reranker_config
from tool.logger import logger

load_dotenv()

# 模型单例对象，避免每次调用都重新加载权重
_reranker = None


def _resolve_device() -> str:
    """
    解析推理设备：
    配置了就用配置的；没配置则自动选择（有 GPU 用 cuda:0，否则用 cpu）
    """
    device = reranker_config.rerank_device
    if device:
        return device

    import torch
    return "cuda:0" if torch.cuda.is_available() else "cpu"


def get_reranker():
    """
    获取本地 Rerank 模型单例对象，首次调用时加载权重
    :return: 初始化完成的 FlagReranker 实例
    """
    global _reranker
    if _reranker is not None:
        return _reranker

    model_path = reranker_config.rerank_model_path

    # 提前校验本地路径，避免被 transformers 抛出一堆难以定位的报错
    if not model_path:
        raise RuntimeError(
            "未配置本地 Rerank 模型路径，请在 .env 中设置 BGE_RERANKER_V2_M3"
        )
    if not os.path.isdir(model_path):
        raise RuntimeError(f"本地 Rerank 模型路径不存在: {model_path}")

    # 延迟导入：FlagEmbedding 会连带加载 torch，放进函数里可以加快模块导入
    from FlagEmbedding import FlagReranker

    device = _resolve_device()
    # CPU 上不支持 fp16，强制关闭，避免报错
    use_fp16 = reranker_config.rerank_fp16 and device != "cpu"

    logger.info(
        f"加载本地 Rerank 模型: {model_path} (device={device}, fp16={use_fp16})"
    )

    _reranker = FlagReranker(
        model_name_or_path=model_path,
        use_fp16=use_fp16,
        devices=device,
        batch_size=reranker_config.rerank_batch_size,
        max_length=reranker_config.rerank_max_length,
        # 归一化到 0~1，与下游断崖阈值（0.5 / 0.25）保持同一量纲
        normalize=reranker_config.rerank_normalize,
    )
    return _reranker


def rerank_documents(query: str, documents: list[str]) -> list[float]:
    """
    使用本地 Cross-Encoder 模型对文档打分
    :param query: 用户查询
    :param documents: 待打分文档内容列表
    :return: 与入参顺序一一对应的相关性分数列表（normalize=True 时为 0~1）
    """
    if not documents:
        return []

    reranker = get_reranker()

    # Cross-Encoder 输入是 (query, passage) 成对，分数越高越相关
    sentence_pairs = [(query, doc) for doc in documents]
    scores = reranker.compute_score(sentence_pairs)

    # 单条文档时 FlagReranker 返回标量，统一成列表
    if isinstance(scores, (int, float)):
        scores = [scores]

    # 保证长度与入参一致，避免下游 zip 时静默错位
    if len(scores) != len(documents):
        raise RuntimeError(
            f"Rerank 打分数量异常: 输入 {len(documents)} 条, 返回 {len(scores)} 条"
        )

    return [float(score) for score in scores]
