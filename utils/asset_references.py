"""仅允许引用同一知识库、同一来源文档的受保护图片。"""
import re
from utils.user_store import get_db

ASSET_URL = re.compile(r"/assets/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![\w/-])")
IMAGE = re.compile(r"!\[[^\]]*\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)")
HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.I)


def sanitize_sources(sources, kb_id):
    """
    保留来源文档中可核验归属的私有图片，清除其他图片地址
    :param sources: 候选来源资料列表
    :param kb_id: 本次操作所属的知识库 ID
    :return: 处理后的来源副本列表，不修改传入记录
    """
    result = []
    for source in sources:
        source = dict(source)
        cache = {}  # 同一来源的重复图片只查询一次资产记录，不跨文档复用判断。
        def allowed(value):
            """
            检查图片地址是否对应当前来源文档的私有资产
            :param value: 待检查的 /assets/资产ID 地址
            :return: 是否允许引用该图片
            """
            url = ASSET_URL.fullmatch(value)
            if source.get("source") != "local" or not url:
                return False
            if value not in cache:
                cache[value] = bool(get_db().assets.find_one({"_id": url[1], "kb_id": kb_id,
                    "document_id": source.get("document_id"), "kind": "image"}))
            return cache[value]

        def valid_image(match):
            """
            保留合法 Markdown 图片，删除不允许的图片标记
            :param match: Markdown 图片正则匹配结果
            :return: 保留的图片标记或空字符串
            """
            return match[0] if allowed(match[1]) else ""

        def html_image(match):
            """
            将合法 HTML 图片转换成 Markdown 图片
            :param match: HTML img 标签的正则匹配结果
            :return: 标准图片标记，或在无权限时返回空字符串
            """
            src = re.search(r'\bsrc=["\']([^"\']+)', match[0], re.I)
            return f'![资料图片]({src[1]})' if src and allowed(src[1]) else ""

        # 先统一 HTML 图片写法，再检查 Markdown 图片及正文中残留的资产地址。
        content = HTML_IMAGE.sub(html_image, source.get("content") or "")
        content = IMAGE.sub(valid_image, content)
        source["content"] = ASSET_URL.sub(lambda match: match[0] if allowed(match[0]) else "", content)
        result.append(source)
    return result
