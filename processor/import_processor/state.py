# knowledge/processor/import_processor/state.py

"""
导入流程状态类型定义

定义完整的状态结构和辅助函数
"""

from typing import TypedDict, List

class ImportGraphState(TypedDict):
    """
    导入流程图状态

    包含整个导入流程中传递的所有数据。
    使用 total=False 表示所有字段都是可选的。
    """

    # ==================== 任务标识 ====================
    user_id: str  # 上传用户 ID，由后台任务记录提供。
    kb_id: str  # 目标知识库 ID，贯穿所有节点的权限检查与写入范围。
    document_id: str  # 当前文档 ID，同名替换时沿用，用任务 ID 区分版本。
    task_id: str                    # 任务 ID，用于任务追踪

    # ==================== 控制标志 ====================
    is_md_read_enabled: bool        # 是否启用 MD 读取
    is_pdf_read_enabled: bool       # 是否启用 PDF 读取

    # ==================== 路径信息 ====================
    import_file_path: str           # 导入文件路径（原始输入）
    file_dir: str                   # 导入(出)文件目录
    pdf_path: str                   # PDF 文件路径
    md_path: str                    # 转换后 Markdown 文件路径

    # ==================== 文件信息 ====================
    file_title: str                 # 文件标题（不含扩展名）
    item_name: str                  # 识别出的商品/产品名称

    # ==================== 处理中间数据 ====================
    md_content: str                 # Markdown 文档内容
    chunks: List                    # 文档切片列表
