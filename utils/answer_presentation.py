"""规范答案引用格式，不修改已保存的旧版会话原文。"""

import re
from urllib.parse import urlsplit


CITATION = re.compile(r"\[cite:(\d+)\]")
LEGACY_CITATION = re.compile(r"[（(]\s*参考(?:内容|资料)?\s*((?:\[\d+\]\s*)+)[）)]")
CODE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`)")
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)")
# 图片展示仅接受受保护的资产路径；具体归属由来源清理及资产下载接口再检查。
PRIVATE_IMAGE = re.compile(r"/assets/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def _map_prose(text, transform):
    return "".join(part if i % 2 else transform(part) for i, part in enumerate(CODE.split(text)))


def clean_legacy_text(text):
    text = re.sub(r"^\s*根据(?:所提供的)?参考内容[，,：:]\s*", "", text)
    return _map_prose(text, lambda part: LEGACY_CITATION.sub(
        lambda match: "".join(f"[cite:{n}]" for n in re.findall(r"\[(\d+)\]", match[1])), part
    ))


def document_images(docs):
    images = []
    for doc in docs:
        if doc.get("source") != "local":
            continue
        for url in MARKDOWN_IMAGE.findall(doc.get("content") or ""):
            if PRIVATE_IMAGE.fullmatch(url) and url not in images:
                images.append(url)
    return images


def present_answer(text, sources, selected_images=None):
    """仅解析实际引用且可用的资料，并通过白名单校验所选图片。"""
    text = clean_legacy_text(text or "")
    image_choices = list(selected_images or [])

    def strip_images(part):
        if "【图片】" in part:
            part, image_block = part.split("【图片】", 1)
            image_choices.extend(PRIVATE_IMAGE.findall(image_block))
        image_choices.extend(MARKDOWN_IMAGE.findall(part))
        return MARKDOWN_IMAGE.sub("", part)

    text = _map_prose(text, strip_images)
    available = {str(source.get("source_id", i)): source for i, source in enumerate(sources or [], 1)}
    used = []

    def resolve(part):
        # 同一段落对同一资料只保留一次引用。
        paragraphs = []
        for paragraph in part.split("\n\n"):
            seen = set()

            def replace(match):
                source_id = match[1]
                if source_id not in available or source_id in seen:
                    return ""
                seen.add(source_id)
                if source_id not in used:
                    used.append(source_id)
                return match[0]

            paragraphs.append(CITATION.sub(replace, paragraph))
        return "\n\n".join(paragraphs)

    text = _map_prose(text, resolve).strip()
    cited = [{**available[source_id], "source_id": source_id} for source_id in used]
    allowed_images = set(document_images(cited))
    images = []
    for url in image_choices:
        if url in allowed_images and url not in images and PRIVATE_IMAGE.fullmatch(url):
            images.append(url)
    return {"answer": text, "sources": cited, "image_urls": images[:3]}


def present_history_message(message):
    if message.get("role") != "assistant":
        return message
    sources = message.get("sources") or []
    selected = message.get("image_urls") if any("source_id" in source for source in sources) else None
    result = present_answer(message.get("text", ""), sources, selected)
    return {**message, "text": result["answer"], "sources": result["sources"], "image_urls": result["image_urls"]}


def history_text(text):
    """清理旧引用编号和图片列表，避免被下一轮对话沿用。"""
    text = clean_legacy_text(text or "")
    return _map_prose(text, lambda part: CITATION.sub("", part.split("【图片】", 1)[0])).strip()
