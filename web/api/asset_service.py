"""通过受保护地址访问私有资产，实际存储路径仅由服务端记录确定。"""
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from utils.knowledge_access import require_kb_permission
from utils.user_store import get_db
from utils.minio_utils import get_minio_client
from minio.error import S3Error
from web.api.auth_dependencies import CurrentUser

router = APIRouter(tags=["私有文件"])


@router.get("/assets/{asset_id}")
def asset(asset_id: str, user: CurrentUser):
    """
    校验知识库权限后，流式返回私有图片或原始文件
    :param asset_id: 资产记录 ID，不接受客户端指定存储桶或对象路径
    :param user: 通过认证依赖取得的当前用户记录
    :return: 图片预览或原文件下载响应
    """
    # 1. 查询资产并校验其知识库归属，校验成功后才访问 MinIO
    record = get_db().assets.find_one({"_id": asset_id})
    if not record:
        raise HTTPException(404, "文件不存在。")
    require_kb_permission(user["_id"], record["kb_id"])
    # 2. 使用服务端登记的桶和对象键读取文件
    try:
        stream = get_minio_client().get_object(record["bucket"], record["object_key"])
    except S3Error as exc:
        raise HTTPException(404 if exc.code == "NoSuchKey" else 503, "文件暂不可用。") from None
    except Exception:
        raise HTTPException(503, "文件存储暂不可用。") from None

    def chunks():
        """
        分块读取对象存储内容，结束或中断时释放连接
        """
        try:
            yield from stream.stream(64 * 1024)
        finally:
            stream.close()
            stream.release_conn()

    # 3. 图片在页面内显示，原文件按附件下载；响应不允许缓存
    mode = "inline" if record["kind"] == "image" else "attachment"
    return StreamingResponse(chunks(), media_type=record["content_type"], headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"{mode}; filename*=UTF-8''{quote(record['filename'], safe='')}",
    })
