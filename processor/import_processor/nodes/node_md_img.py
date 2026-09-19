from utils.knowledge_store import store_asset
# processor/import_processor/nodes/node_md_img.py
import base64
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Tuple, List, Dict
from langchain_openai import ChatOpenAI
from minio import Minio
from minio.deleteobjects import DeleteObject
from config.lm_config import lm_config
from config.minio_config import minio_config
from config.import_config import import_config
from processor.import_processor.base import BaseNode
from processor.import_processor.state import ImportGraphState
from utils.minio_utils import get_minio_client
from tool.logger import logger

# MD 中图片引用的两种形态：MinerU 对正文配图输出 markdown 语法，对表格单元格内的小图标输出 HTML 标签
_IMAGE_REF_MD = r"!\[[^\]]*?\]\([^)]*?{file}[^)]*?\)"
_IMAGE_REF_HTML = r"<img\b[^>]*?src=[\"'][^\"']*?{file}[^\"']*?[\"'][^>]*>"

# HTML 形态 alt 文本限长：表格单元格空间有限，摘要过长会撑破表格布局
_HTML_ALT_MAX = 30

class NodeMDImg(BaseNode):
    """
    MarkDown图片处理节点：多模态图片理解
    """

    name = "node_md_img"

    def process(self, state: ImportGraphState):

        """
        MD文件图片处理核心节点
        核心流程：
        1. 获取MD内容、文件路径、图片文件夹路径
        2. 扫描图片文件夹，筛选MD中实际引用的支持格式图片
        3. 调用多模态大模型为图片生成内容摘要
        4. 将图片上传至MinIO，替换MD中本地图片路径为MinIO访问URL，并填充图片摘要
        5. 备份原MD文件，保存处理后的新MD文件并更新状态

        :param state: md_path、md_content
        :return: md_path、md_content
        """

        # 步骤1：初始化数据，获取MD核心信息
        md_content, md_path_obj, images_dir = self._step_1_get_content(state)
        if not images_dir.exists():
            logger.info("无图片文件夹，跳过图片处理")
            return state

        # 步骤2：扫描并筛选MD中引用的图片
        target_images = self._step_2_scan_images(md_content, images_dir)
        if not target_images:
            logger.info("未检测到MD中引用了图片，跳过图片处理")
            return state

        # 步骤3：调用多模态大模型生成图片摘要
        summaries = self._step_3_generate_summaries(md_path_obj.stem, target_images)

        # 步骤4：上传图片至MinIO，替换MD图片路径并填充摘要
        new_md_content = self._step_4_upload_and_replace(state, target_images, summaries, md_content)

        # 步骤5：备份并保存新MD文件
        new_md_file_name = self._step_5_backup_new_md_file(state['md_path'], new_md_content)

        # 步骤6：更新state状态值
        state["md_content"] = new_md_content
        state["md_path"] = new_md_file_name

        return state

    def _step_1_get_content(self, state: ImportGraphState) -> Tuple[str, Path, Path]:
        """
        从全局状态中提取并初始化MD处理所需核心数据
        :param state: 流程全局状态对象
        :return: 元组(MD文件内容, MD文件路径, 图片文件夹路径)
        :raise FileProcessingError: 当状态中无有效MD文件路径时抛出
        """

        # 1、参数非空校验
        md_path = state.get("md_path")
        if not md_path:
            raise ValueError(f'{md_path}不能为空')

        # 2、路径转换
        md_path_obj = Path(md_path)

        # 3、检查PDF文件的有效性
        if not md_path_obj.exists():
            raise ValueError(f"MD文件{md_path_obj.name}不存在")

        # 4、获取md_content
        md_content = state["md_content"]

        # 5、组装图片文件夹路径：图片文件夹固定为MD文件同级的images目录
        images_dir = md_path_obj.parent / "images"

        return md_content, md_path_obj, images_dir

    def _step_2_scan_images(self, md_content: str, images_dir: Path) -> List[Tuple[str, str, Tuple[str, str]]]:
        """
        扫描图片文件夹，过滤出「支持格式+MD中实际引用」的图片，组装处理元数据
        :param md_content: MD文件完整内容
        :param images_dir: 图片文件夹路径对象
        :return: 待处理图片列表，每个元素为(图片文件名, 图片完整路径, 图片上下文)元组
        """

        # 1. 定义待处理图片列表
        target_images = []

        # 2. 遍历图片文件夹
        for image_file in os.listdir(images_dir):

            # 2.1 过滤无效后缀
            file_ext = os.path.splitext(image_file)[1].lower()
            if file_ext not in import_config.image_extensions:
                logger.warning(f"图片格式不支持，跳过：{image_file}")
                continue

            # 1.2 组装图片完整路径并转成字符串
            img_path = str(images_dir / image_file)

            # 1.3 查找图片在MD中的引用上下文
            context = self._find_image_in_md(md_content, image_file)

            # 过滤MD中未引用的图片
            if not context:
                logger.warning(f"图片未在MD中引用，跳过处理：{image_file}")
                continue

            # 1.4 组装待处理图片元数据，取第一个匹配的图片上下文
            target_images.append((image_file, img_path, context))

        return target_images

    def _iter_image_refs(self, md_content: str, image_file: str) -> List[Tuple[str, int, int]]:
        """
        统一发现MD中指定图片的全部引用位置，同时识别 markdown 与 HTML 两种形态
        :param md_content: MD文件完整内容
        :param image_file: 图片文件名（含后缀）
        :return: 引用列表，元素为(形态, 起始下标, 结束下标)，按位置升序；形态取 'md' 或 'html'
        """
        escaped = re.escape(image_file)
        refs = []
        for match in re.finditer(_IMAGE_REF_MD.format(file=escaped), md_content):
            refs.append(("md", match.start(), match.end()))
        for match in re.finditer(_IMAGE_REF_HTML.format(file=escaped), md_content):
            refs.append(("html", match.start(), match.end()))
        return sorted(refs, key=lambda item: item[1])

    def _find_image_in_md(self, md_content: str, image_file: str, context_len: int = 100) -> Tuple[str, str]:
        """
        查找MD内容中指定图片的引用位置，并返回首个引用的上下文文本
        :param md_content: MD文件完整内容
        :param image_file: 图片文件名（含后缀）
        :param context_len: 上下文截取长度，默认前后各100字符
        :return: (上文, 下文)元组，无匹配则返回None
        """
        refs = self._iter_image_refs(md_content, image_file)
        if not refs:
            return None  # 没有找到

        # 截取首个引用位置的上文和下文（防止索引越界）
        _, start, end = refs[0]
        pre_text = md_content[max(0, start - context_len):start]
        post_text = md_content[end:min(len(md_content), end + context_len)]

        return pre_text, post_text

    def _step_3_generate_summaries(self, doc_stem: str, target_images: List[Tuple[str, str, Tuple[str, str]]]) -> Dict[
        str, str]:
        """
        步骤3：以最多5路并发为图片生成摘要，按完成顺序记录进度
        :param doc_stem: 文档文件名（不含后缀），作为大模型prompt上下文
        :param target_images: 待处理图片列表，元素为(图片文件名, 图片完整路径, 图片上下文)
        :return: 图片摘要字典，键：图片文件名，值：图片内容摘要
        """
        summaries = {}

        total = len(target_images)
        if not total:
            return summaries

        # 1、完整工作流仍串行执行，仅在本节点并发请求图片模型。
        # 并发表示同时处理的请求数，不是每分钟次数；使用5路为当前10路额度留出余量。
        logger.info(f"开始生成图片摘要：{doc_stem}，共{total}张，最多5路并发")
        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="image-summary") as executor:
            # 2、保存任务与图片文件名的对应关系，避免返回顺序不同导致摘要串图。
            futures = {
                executor.submit(
                    self._summarize_image, image_path, root_folder=doc_stem, image_content=context
                ): img_file
                for img_file, image_path, context in target_images
            }

            # 3、由当前线程统一收集结果；全部完成后再交给后续步骤上传和替换MD。
            # 完成数包含调用失败后使用默认描述的图片，具体错误沿用原有日志记录。
            for completed, future in enumerate(as_completed(futures), start=1):
                summaries[futures[future]] = future.result()
                logger.info(f"图片处理完成 {completed}/{total}：{doc_stem}，{futures[future]}")

        return summaries

    def _summarize_image(self, image_path: str, root_folder: str, image_content: Tuple[str, str]) -> str:
        """
           调用多模态大模型总结图片内容。

           参数：
           - image_path: 图片本地路径。
           - root_folder: 文档所属文件夹名（提供更多上下文）。
           - image_content: 图片在文档中的上下文 (前文, 后文)。
        """
        with open(image_path, "rb") as img_file:
            base64_image = base64.b64encode(img_file.read()).decode("utf-8")

        try:
            chat_model = ChatOpenAI(
                model=lm_config.vl_model,
                api_key=lm_config.api_key,
                base_url=lm_config.base_url,
                temperature=lm_config.llm_temperature,
                timeout=90,
                max_retries=1,
            )
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"""这是"{root_folder}"文件中的一张图片，图片上文部分为"{image_content[0]}"，下文部分为"{image_content[1]}"，请用中文简要总结这张图片的内容，用于 Markdown 图片标题。"""
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ]
            response = chat_model.invoke(messages)
            return response.content.strip().replace("\n", "")

        except Exception as e:
            logger.error(f"图像总结失败：{image_path}, 错误{e}")
            return "图片描述"

    def _step_4_upload_and_replace(self, state, target_images, summaries, md_content):
        """
        上传图片到私有存储，并将 Markdown 图片引用替换为受保护地址
        :param state: 含用户、知识库、文档和任务 ID 的导入状态
        :param target_images: 待处理图片的文件名、路径及附加信息列表
        :param summaries: 以图片文件名为键的摘要字典
        :param md_content: 待替换图片引用的 Markdown 正文
        :return: 合并图片摘要和私有资产地址后的 Markdown 内容
        """
        # 1. 将图片登记到本次文档和导入版本下，不生成公开桶地址
        urls = {}
        for filename, path, _ in target_images:
            urls[filename] = store_asset(state["user_id"], state["kb_id"], state["document_id"],
                                         state["task_id"], path, "image")
        # 2. 合并摘要和地址，再替换正文中的图片引用
        return self._process_md_file(md_content, self._merge_summary_and_url(summaries, urls))

    def _merge_summary_and_url(self, summaries: Dict[str, str], urls: Dict[str, str]) -> Dict[str, Tuple[str, str]]:
        """
        合并图片摘要字典和URL字典，过滤掉上传失败无URL的图片
        :param summaries: 图片摘要字典，键：图片文件名，值：内容摘要
        :param urls: 图片URL字典，键：图片文件名，值：MinIO访问URL
        :return: 合并后的图片信息字典，键：图片文件名，值：(摘要, URL)元组
        """
        image_info = {}
        for image_file, summary in summaries.items():
            if url := urls.get(image_file):
                image_info[image_file] = (summary, url)
        return image_info

    @staticmethod
    def _escape_html_attr(text: str) -> str:
        """
        转义HTML属性值，防止摘要中的引号/尖括号截断标签
        :param text: 待转义文本
        :return: 可安全放入双引号属性值的文本
        """
        return (text.replace("&", "&amp;").replace('"', "&quot;")
                    .replace("<", "&lt;").replace(">", "&gt;"))

    def _process_md_file(self, md_content: str, image_info: Dict[str, Tuple[str, str]]) -> str:
        """
        核心功能：替换MD内容中的本地图片引用为MinIO远程引用
        替换规则：
            1. markdown形态：![原描述](本地路径)    → ![图片摘要](MinIO访问URL)
            2. HTML形态    ：<img src="本地路径"/>  → <img src="MinIO访问URL" alt="图片摘要"/>
        HTML形态必须保留标签结构：<table> 内的 markdown 语法不会被解析，替换成 ![..]() 会渲染为字面量并破坏表格
        :param md_content: 原始MD文件内容
        :param image_info: 合并后的图片信息字典，键：图片文件名，值：(摘要, URL)
        :return: 替换后的新MD内容
        """

        # 1、先收集全部待替换区间，避免边替换边匹配导致下标错乱
        edits = []
        for image_file, (summary, new_url) in image_info.items():
            for kind, start, end in self._iter_image_refs(md_content, image_file):
                if kind == "md":
                    repl = f"![{summary}]({new_url})"
                else:
                    alt = summary if len(summary) <= _HTML_ALT_MAX else summary[:_HTML_ALT_MAX]
                    repl = f'<img src="{self._escape_html_attr(new_url)}" alt="{self._escape_html_attr(alt)}"/>'
                edits.append((start, end, repl))

        # 2、按下标倒序替换，保证前面的下标不受影响
        for start, end, repl in sorted(edits, key=lambda item: item[0], reverse=True):
            md_content = md_content[:start] + repl + md_content[end:]

        logger.info(f"MD文件图片引用替换完成，共替换{len(edits)}处图片引用")

        return md_content

    def _step_5_backup_new_md_file(self, origin_md_path: str, md_content: str) -> str:
        """
        步骤5：将处理后的MD内容保存为新文件（原文件不变，避免数据丢失）
        新文件命名规则：原文件名 + _new.md（如test.md → test_new.md）
        :param origin_md_path: 原始MD文件完整路径
        :param md_content: 处理后的新MD内容
        :return: 新MD文件的完整路径
        """
        # 构造新文件路径：替换原后缀为 _new.md
        new_md_file_name = os.path.splitext(origin_md_path)[0] + "_new.md"

        # 写入新MD内容（覆盖写入，若文件已存在则更新）
        with open(new_md_file_name, "w", encoding="utf-8") as f:
            f.write(md_content)

        logger.info(f"处理后MD文件已保存，新文件路径：{new_md_file_name}")

        return new_md_file_name

if __name__ == "__main__":

    md_path = r"D:\output\hak180产品安全手册\hak180产品安全手册.md"
    with open(md_path, "r", encoding="utf-8") as f:
        md_content = f.read()

    init_state = {
        "md_path": md_path,
        "md_content": md_content
    }

    # 执行核心处理流程
    node_md_img = NodeMDImg()
    result = node_md_img(init_state)

    logger.info(json.dumps(result, ensure_ascii=False, indent=4))
