"""Minimal MCP server over stdio: newline-delimited JSON-RPC 2.0.

Handles initialize, notifications/initialized, ping, tools/list and tools/call,
and can send elicitation/create requests to the client (Approve/Cancel prompts).
Requests are handled concurrently so a tool call can wait for an elicitation
answer while the reader keeps reading.
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback

from .config import SERVER_NAME, SERVER_VERSION
from .tools import TOOLS

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_VERSION = "2025-06-18"

INSTRUCTIONS = (
    "IBKR PAPER trading through the local IB Gateway. Orders go to IBKR paper accounts (DU…/DF…) "
    "only. To trade: call preview_order, show the user the read-back, then call place_order with "
    "the preview_id and confirm: true. Never call place_order without first showing the preview."
)


def log(msg: str) -> None:
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)


class StdioServer:
    def __init__(self, app, write):
        """write: callable(str) that writes one line (without newline handling) to the client."""
        self.app = app
        self._write = write
        self.client_caps: dict = {}
        self._pending: dict = {}
        self._next_id = 0
        self._tasks: set = set()
        app.elicit = self.elicit
        app.elicitation_supported = self.elicitation_supported

    # -- output -------------------------------------------------------------
    def send(self, msg: dict) -> None:
        self._write(json.dumps(msg, separators=(",", ":"), default=str))

    def _reply(self, mid, result=None, error=None) -> None:
        msg = {"jsonrpc": "2.0", "id": mid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        self.send(msg)

    # -- elicitation ----------------------------------------------------------
    def elicitation_supported(self) -> bool:
        return isinstance(self.client_caps.get("elicitation"), dict)

    async def elicit(self, message: str, schema: dict):
        if not self.elicitation_supported():
            return None
        self._next_id += 1
        rid = f"srv-{self._next_id}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self.send({
            "jsonrpc": "2.0", "id": rid, "method": "elicitation/create",
            "params": {"message": message, "requestedSchema": schema},
        })
        try:
            return await asyncio.wait_for(fut, timeout=self.app.cfg.elicit_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return {"action": "cancel"}
        finally:
            self._pending.pop(rid, None)

    # -- input ----------------------------------------------------------------
    def handle_line(self, line) -> None:
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            self._reply(None, error={"code": -32700, "message": "Parse error"})
            return
        if isinstance(msg, list):  # batches (2025-03-26) — handle each element
            for m in msg:
                self._dispatch(m)
            return
        self._dispatch(msg)

    def _dispatch(self, msg) -> None:
        if not isinstance(msg, dict):
            self._reply(None, error={"code": -32600, "message": "Invalid Request"})
            return
        method = msg.get("method")
        mid = msg.get("id")
        if method is None:
            fut = self._pending.get(mid) if mid is not None else None
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_result({"action": "cancel", "error": msg["error"]})
                else:
                    fut.set_result(msg.get("result"))
            return
        if mid is None:
            return  # notification (notifications/initialized, cancelled, ...)
        task = asyncio.get_running_loop().create_task(self._handle_request(mid, method, msg.get("params") or {}))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_request(self, mid, method, params) -> None:
        try:
            if method == "initialize":
                caps = params.get("capabilities") if isinstance(params, dict) else None
                self.client_caps = caps if isinstance(caps, dict) else {}
                requested = params.get("protocolVersion") if isinstance(params, dict) else None
                version = requested if requested in SUPPORTED_VERSIONS else DEFAULT_VERSION
                self._reply(mid, {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": INSTRUCTIONS,
                })
            elif method == "ping":
                self._reply(mid, {})
            elif method == "tools/list":
                self._reply(mid, {"tools": TOOLS})
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                try:
                    res = await self.app.call(name, args)
                except KeyError:
                    self._reply(mid, error={"code": -32602, "message": f"Unknown tool: {name}"})
                    return
                self._reply(mid, res)
            elif method in ("resources/list", "prompts/list"):
                self._reply(mid, {method.split("/")[0]: []})
            else:
                self._reply(mid, error={"code": -32601, "message": f"Method not found: {method}"})
        except Exception as e:  # never crash the server on one bad call
            log("internal error: " + "".join(traceback.format_exception(type(e), e, e.__traceback__)))
            self._reply(mid, error={"code": -32603, "message": f"Internal error: {e}"})

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


async def serve_stdio(app) -> None:
    """Run on the real process stdin/stdout. stdout is reserved for JSON-RPC."""
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # stray prints from libraries can never corrupt the protocol

    def write(text: str) -> None:
        out.write(text.encode("utf-8") + b"\n")
        out.flush()

    server = StdioServer(app, write)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def reader() -> None:
        try:
            for raw in iter(sys.stdin.buffer.readline, b""):
                loop.call_soon_threadsafe(queue.put_nowait, raw)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    import threading

    threading.Thread(target=reader, name="stdin-reader", daemon=True).start()
    log(f"{SERVER_VERSION} ready (stdio)")
    while True:
        raw = await queue.get()
        if raw is None:
            break
        server.handle_line(raw)
    await server.drain()
    app.broker.close()
