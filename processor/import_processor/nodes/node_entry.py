# processor/import_processor/nodes/node_entry.py
from pathlib import Path
from processor.import_processor.base import BaseNode
from processor.import_processor.state import ImportGraphState


class NodeEntry(BaseNode):
    """
    入口节点：任务分发
    """

    name = "node_entry"

    def process(self, state: ImportGraphState):
        #1. 从state中获取文件的绝对路径
        import_file_path = state.get("import_file_path")
        # 判断路径是否为空
        if not import_file_path:
            raise ValueError (f"{import_file_path}为空路径")
        #2. 将 import_file_path 转换为Path对象
        import_file_path_obj = Path(import_file_path)

        if not import_file_path_obj.exists():
            raise ValueError(f"文件{import_file_path_obj.name}不存在")

        # 3. 判断文件类型
        if import_file_path_obj.suffix == ".pdf":
            state["is_pdf_read_enabled"] = True
            state["pdf_path"] = import_file_path
        elif import_file_path_obj.suffix == ".md":
            state["is_md_read_enabled"] = True
            state["md_path"] = import_file_path
        else:
            raise ValueError(f"不支持的文件类型{import_file_path_obj.suffix}")

        # 4. 获取文件名作为标题
        state["file_title"] = import_file_path_obj.stem

        return state
