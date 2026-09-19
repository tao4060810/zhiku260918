"""账号配置；不读取或保存管理员密码。"""
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class AuthConfig:
    """从环境变量读取不可变的认证配置，启动时统一校验。"""
    production: bool = os.getenv("APP_ENV", "development") == "production"
    secure: bool = os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true"
    registration: bool = os.getenv("AUTH_REGISTRATION_ENABLED", "true").lower() == "true"
    ttl: int = int(os.getenv("AUTH_SESSION_TTL_SECONDS", "86400"))  # 登录凭证的绝对有效期，单位秒。
    idle: int = int(os.getenv("AUTH_IDLE_TTL_SECONDS", "7200"))  # 无用户活动时的失效时间，单位秒。
    origins: tuple = tuple(x.strip().rstrip("/") for x in os.getenv(
        "AUTH_ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000"
    ).split(",") if x.strip())
    cookie: str = "zhiku_auth"

    def validate(self):
        """
        校验登录有效期、允许的站点来源和生产环境 Cookie 配置
        """
        # 1. 校验有效期和来源列表，避免空配置绕过后续来源检查
        if self.ttl <= 0 or self.idle <= 0 or not self.origins:
            raise ValueError("认证有效期和站点来源配置无效")
        # 2. 来源只包含协议、主机和可选端口，不接受路径、参数或账号信息
        for origin in self.origins:
            url = urlsplit(origin)
            if url.scheme not in {"http", "https"} or not url.netloc or url.path or url.query or url.fragment or url.username:
                raise ValueError("AUTH_ALLOWED_ORIGINS 必须是完整站点源")
        # 3. 生产环境要求 HTTPS 来源和只经 HTTPS 发送的 Cookie
        if self.production and (not self.secure or any(not x.startswith("https://") for x in self.origins)):
            raise ValueError("生产模式必须使用 HTTPS 和 Secure Cookie")


auth_config = AuthConfig()
auth_config.validate()
