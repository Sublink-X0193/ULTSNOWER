from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root / "src"))
    from merchant_portal_server.db import Database
    from merchant_portal_server.service import MerchantService

    db_path = Path(os.environ.get("MERCHANT_DB_PATH", str(repo_root / "data" / "merchant.sqlite")))
    service = MerchantService(Database(db_path), bridge_client=None)
    service.ensure_default_admin(os.environ.get("MERCHANT_ADMIN_USERNAME", "admin"), os.environ.get("MERCHANT_ADMIN_PASSWORD", "admin123456"))
    for code, minutes in [("TEST-60", 60), ("TEST-180", 180), ("TEST-600", 600)]:
        service.add_recharge_card(code, minutes=minutes)
    print(f"Seeded merchant DB: {db_path}")
    print("Admin: admin / admin123456")
    print("Recharge cards: TEST-60, TEST-180, TEST-600")


if __name__ == "__main__":
    main()
