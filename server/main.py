"""ibkr-paper-gateway: MCP stdio server for IBKR PAPER trading via the local IB Gateway.

Run: uv run --no-project --quiet --with "ib_async>=2.0" python server/main.py
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if sys.version_info < (3, 11):  # pragma: no cover
    sys.exit("ibkr-paper-gateway needs Python 3.11 or newer")


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="[ibkr-paper-gateway] %(name)s %(levelname)s: %(message)s")
    from ibkr_paper.config import Config
    from ibkr_paper.rpc import serve_stdio
    from ibkr_paper.tools import App

    app = App(Config.from_env())
    try:
        asyncio.run(serve_stdio(app))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
