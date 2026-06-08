from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    bridge_src = Path(os.environ.get("SNOW_DEVICE_CONTROL_BRIDGE_SRC", r"C:\Users\WS\Documents\SNOW_DEVICE_CONTROL_BRIDGE\src"))
    sys.path.insert(0, str(bridge_src))

    from device_control_bridge.app import create_app

    db_path = Path(os.environ.get("BRIDGE_DB", str(repo_root / "data" / "dev_bridge.sqlite")))
    app = create_app(db_path)
    bridge = app.state.bridge
    scopes = {
        "machines.read",
        "machines.control",
        "commands.read",
        "commands.write",
        "sessions.read",
        "sessions.write",
    }
    bridge_secret = os.environ.get("BRIDGE_MERCHANT_SECRET", "dev_bridge_merchant_secret")
    bridge.create_api_key(5782, os.environ.get("BRIDGE_MERCHANT_KEY", "mk_test"), bridge_secret, scopes, name="dev merchant")
    for i in range(1, int(os.environ.get("DEV_BRIDGE_DEVICE_COUNT", "3")) + 1):
        bridge.register_device(5782, f"dev-machine-{i}", f"开发测试机器 {i}", online=True)

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8010"))
    print(f"Seeded Device Control Bridge on http://{host}:{port}")
    print(f"DB: {db_path}")
    print(f"API key: mk_test / {bridge_secret}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
