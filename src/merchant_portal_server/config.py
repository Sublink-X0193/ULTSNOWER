from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path(os.getenv("MERCHANT_DB_PATH", "data/merchant.sqlite"))
    bridge_base_url: str = os.getenv("BRIDGE_BASE_URL", "http://127.0.0.1:8010")
    bridge_merchant_key: str = os.getenv("BRIDGE_MERCHANT_KEY", "mk_test")
    bridge_merchant_secret: str = os.getenv("BRIDGE_MERCHANT_SECRET", "secret")
    merchant_ref_secret: str = os.getenv("MERCHANT_REF_SECRET", "dev-merchant-ref-secret-change-me")
    session_ttl_seconds: int = int(os.getenv("MERCHANT_SESSION_TTL_SECONDS", "86400"))
    enable_background_workers: bool = os.getenv("MERCHANT_ENABLE_BACKGROUND_WORKERS", "0") in {"1", "true", "TRUE", "yes"}
    bind_host: str = os.getenv("MERCHANT_HOST", "127.0.0.1")
    bind_port: int = int(os.getenv("MERCHANT_PORT", "8020"))


def load_settings() -> Settings:
    return Settings()
