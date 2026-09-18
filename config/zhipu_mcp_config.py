# config/bailian_mcp_config.py

# 导入核心依赖：数据类、环境变量读取、路径处理
from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()


# 定义mcp的服务配置
@dataclass
class McpConfig:
    mcp_base_url: str
    api_key : str

# 注意使用aliyun的api_key
mcp_config = McpConfig(
    mcp_base_url=os.getenv("MCP_ZHIPU_BASE_URL"),
    api_key=os.getenv("MCP_ZHIPU_API_KEY") or os.getenv("OPENAI_API_KEY")
)
