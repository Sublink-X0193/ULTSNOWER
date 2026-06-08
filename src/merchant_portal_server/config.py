from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path(os.getenv("MERCHANT_DB_PATH", "data/merchant.sqlite"))
    bridge_base_url: str = os.getenv("BRIDGE_BASE_URL", "http://127.0.0.1:8010")
    bridge_merchant_key: str = os.getenv("BRIDGE_MERCHANT_KEY", "mk_test")
    bridge_merchant_secret: str = os.getenv("BRIDGE_MERCHANT_SECRET", "dev_bridge_merchant_secret")
    bridge_api_prefix: str = os.getenv("BRIDGE_API_PREFIX", "/api/external/v1")
    bridge_auth_header_prefix: str = os.getenv("BRIDGE_AUTH_HEADER_PREFIX", "External")
    merchant_ref_secret: str = os.getenv("MERCHANT_REF_SECRET", "dev-merchant-ref-secret-change-me")
    session_ttl_seconds: int = int(os.getenv("MERCHANT_SESSION_TTL_SECONDS", "86400"))
    enable_background_workers: bool = os.getenv("MERCHANT_ENABLE_BACKGROUND_WORKERS", "0") in {"1", "true", "TRUE", "yes"}
    internal_worker_token: str = os.getenv("MERCHANT_INTERNAL_WORKER_TOKEN", "")
    bind_host: str = os.getenv("MERCHANT_HOST", "127.0.0.1")
    bind_port: int = int(os.getenv("MERCHANT_PORT", "8020"))
    default_admin_username: str = os.getenv("MERCHANT_ADMIN_USERNAME", "admin")
    default_admin_password: str = os.getenv("MERCHANT_ADMIN_PASSWORD", "change_me_before_production")
    # 测试/拆分联调阶段默认不强制首启 Bridge API Key 配置；
    # 正式部署时设置 MERCHANT_REQUIRE_BRIDGE_SETUP=1 才会拦截全站进入 /setup。
    require_bridge_setup: bool = os.getenv("MERCHANT_REQUIRE_BRIDGE_SETUP", "0") in {"1", "true", "TRUE", "yes"}


def load_settings() -> Settings:
    return Settings()
