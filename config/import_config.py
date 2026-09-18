# config/import_config.py

from dataclasses import dataclass
import os
from typing import Set
from dotenv import load_dotenv

load_dotenv()

@dataclass
class ImportConfig:
    max_content_length: int
    min_content_length: int
    item_name_chunk_k: int
    item_name_chunk_size: int
    image_extensions: Set[str]

# 实例化Import配置对象（和其他配置对象命名风格统一）
import_config = ImportConfig(
    max_content_length=int(os.getenv("MAX_CONTENT_LENGTH")),
    min_content_length=int(os.getenv("MIN_CONTENT_LENGTH")),
    item_name_chunk_k=int(os.getenv("ITEM_NAME_CHUNK_K")),
    item_name_chunk_size=int(os.getenv("ITEM_NAME_CHUNK_SIZE")),
    image_extensions={ext.strip() for ext in os.getenv("IMAGE_EXTENSIONS", ".jpg,.jpeg,.png,.gif,.bmp,.webp").split(",")},
)
