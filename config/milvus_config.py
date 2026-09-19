# config/milvus_config.py

from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass
class MilvusConfig:
    milvus_url: str
    chunks_collection: str
    item_name_collection: str

# 实例化Milvus配置对象（和其他配置对象命名风格统一）
milvus_config = MilvusConfig(
    milvus_url=os.getenv("MILVUS_URL"),
    # 私有集合与旧公共集合分开，结构中必须包含知识库、文档和导入版本字段。
    chunks_collection=os.getenv("PRIVATE_CHUNKS_COLLECTION", "kb_private_chunks_v1"),
    item_name_collection=os.getenv("PRIVATE_ITEM_NAME_COLLECTION", "kb_private_items_v1")
)
