"""只允许无公开策略的私有桶，不自动修改已有桶权限。"""
from threading import Lock
import os
import urllib3
from minio import Minio
from minio.error import S3Error
from config.minio_config import minio_config

_client = None
_lock = Lock()


def get_minio_client():
    """
    复用 MinIO 客户端，确保私有桶存在且未配置访问策略
    :return: 通过私有桶检查的 MinIO 客户端
    """
    global _client
    with _lock:
        if _client is None:
            client = Minio(minio_config.endpoint, access_key=minio_config.access_key,
                secret_key=minio_config.secret_key, secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
                http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=5, read=30), retries=False))
            if not client.bucket_exists(minio_config.bucket_name):
                client.make_bucket(minio_config.bucket_name)
            _client = client
        # 每次业务操作检查，不让运行期间的公开策略变更静默生效。
        try:
            policy = _client.get_bucket_policy(minio_config.bucket_name)
        except S3Error as exc:
            if exc.code != "NoSuchBucketPolicy":
                raise
        else:
            if policy:
                raise RuntimeError("私有桶存在访问策略，请先确认并移除公开读取策略")
        return _client
