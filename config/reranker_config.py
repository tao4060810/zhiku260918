# config/reranker_config.py

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _to_bool(value) -> bool:
    """把 .env 里常见的 1/0、True/False、yes/no 等写法统一转成布尔值"""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class RerankerConfig:
    rerank_model_path: str   # 本地模型路径
    rerank_device: str       # 推理设备，如 cuda:0 / cpu
    rerank_fp16: bool        # 半精度
    rerank_max_length: int   # 单对 query+passage 的最大 token 数
    rerank_batch_size: int   # 推理批大小
    rerank_normalize: bool   # 是否用 sigmoid 把分数归一化到 0~1

reranker_config = RerankerConfig(
    rerank_model_path=os.getenv("BGE_RERANKER_V2_M3"),
    rerank_device=os.getenv("BGE_RERANKER_DEVICE", "cpu"),
    rerank_fp16=_to_bool(os.getenv("BGE_RERANKER_FP16", "1")),
    rerank_max_length=int(os.getenv("BGE_RERANKER_MAX_LENGTH", "2048")),
    rerank_batch_size=int(os.getenv("BGE_RERANKER_BATCH_SIZE", "16")),
    rerank_normalize=_to_bool(os.getenv("BGE_RERANKER_NORMALIZE", "1")),
)
