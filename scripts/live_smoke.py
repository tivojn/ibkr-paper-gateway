"""Live smoke check (read-only): is IB Gateway listening on 127.0.0.1:4002, and what does
gateway_status report? Places no orders and calls no order tools.

Run: uv run --no-project --quiet --with "ib_async>=2.0" python scripts/live_smoke.py
"""

import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

from ibkr_paper.config import Config  # noqa: E402
from ibkr_paper.tools import App  # noqa: E402


def listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


async def main() -> int:
    cfg = Config.from_env()
    host = "127.0.0.1"
    if not listening(host, cfg.port):
        print(f"Nothing is listening on {host}:{cfg.port}. Start IB Gateway, log in with your PAPER "
              "login, and enable the API on port 4002 (Read-Only API off).")
        return 2
    print(f"Something is listening on {host}:{cfg.port}; asking for gateway_status (read-only)...")
    app = App(cfg)
    try:
        res = await app.call("gateway_status", {})
        print(res["content"][0]["text"])
    finally:
        app.broker.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
