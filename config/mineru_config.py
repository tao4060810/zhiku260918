import os
from dataclasses import dataclass
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv(override=True) # 优先加载项目配置

@dataclass
class MinerUConfig:
    base_url: str
    api_token: str

mineru_config = MinerUConfig(
    base_url=os.getenv("MINERU_BASE_URL", ""),
    api_token=os.getenv("MINERU_API_TOKEN", "")
)