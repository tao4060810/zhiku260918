# config/minio_config.py

from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass
class MinIOConfig:
    endpoint: str
    access_key: str
    secret_key: str
    bucket_name: str
    img_dir: str

minio_config = MinIOConfig(
    endpoint=os.getenv("MINIO_ENDPOINT"),
    access_key=os.getenv("MINIO_ACCESS_KEY"),
    secret_key=os.getenv("MINIO_SECRET_KEY"),
    # 新资产写入私有桶，通过 Web 鉴权接口访问，不复用旧公开图片桶。
    bucket_name=os.getenv("MINIO_PRIVATE_BUCKET", "zhiku-private"),
    img_dir=os.getenv("MINIO_IMG_DIR"),
)
