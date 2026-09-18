"""Initialize object storage on demand, with bounded network timeouts."""

import json
from threading import Lock

import urllib3
from minio import Minio

from config.minio_config import minio_config

_client = None
_lock = Lock()


def get_minio_client():
    global _client
    with _lock:
        if _client is not None:
            return _client
        client = Minio(
            endpoint=minio_config.endpoint,
            access_key=minio_config.access_key,
            secret_key=minio_config.secret_key,
            secure=False,
            http_client=urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=5, read=30), retries=False,
            ),
        )
        if not client.bucket_exists(minio_config.bucket_name):
            client.make_bucket(minio_config.bucket_name)
            prefix = (minio_config.img_dir or "images").strip("/")
            client.set_bucket_policy(minio_config.bucket_name, json.dumps({
                "Version": "2012-10-17", "Statement": [{
                    "Effect": "Allow", "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{minio_config.bucket_name}/{prefix}/*"],
                }],
            }))
        _client = client
        return client
