import asyncio
import json
import os
import socket
import subprocess
import sys
import unittest

from ibkr_paper.rpc import StdioServer

from .helpers import make_app

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server", "main.py")


class InProcessRoundTrip(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = make_app()
        self.out: asyncio.Queue = asyncio.Queue()
        self.server = StdioServer(self.app, lambda text: self.out.put_nowait(json.loads(text)))

    def send(self, msg):
        self.server.handle_line(json.dumps(msg).encode())

    async def recv(self):
        return await asyncio.wait_for(self.out.get(), 5)

    async def request(self, mid, method, params=None):
        self.send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        return await self.recv()

    async def test_round_trip_with_elicitation(self):
        init = await self.request(1, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {"elicitation": {}},
            "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init["result"]["serverInfo"]["name"], "ibkr-paper-gateway")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((await self.request(2, "ping"))["result"], {})
        tools = (await self.request(3, "tools/list"))["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"gateway_status", "account_summary", "positions", "portfolio", "quote",
                                 "history", "open_orders", "executions", "preview_order", "place_order",
                                 "cancel_order", "modify_order", "paper_log"})
        place = next(t for t in tools if t["name"] == "place_order")
        self.assertTrue(place["annotations"]["destructiveHint"])

        prev = await self.request(4, "tools/call", {"name": "preview_order", "arguments": {
            "symbol": "AAPL", "action": "BUY", "quantity": 10, "orderType": "LMT", "limitPrice": 100}})
        text = prev["result"]["content"][0]["text"]
        pid = json.loads(text.split("\n", 1)[1])["preview_id"]

        self.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                   "params": {"name": "place_order", "arguments": {"preview_id": pid, "confirm": True}}})
        ask = await self.recv()
        self.assertEqual(ask["method"], "elicitation/create")
        self.assertIn("BUY 10", ask["params"]["message"])
        self.send({"jsonrpc": "2.0", "id": ask["id"], "result": {"action": "accept",
                                                                 "content": {"decision": "Approve"}}})
        done = await self.recv()
        self.assertEqual(done["id"], 5)
        self.assertNotIn("isError", done["result"])
        self.assertEqual(len(self.app.fake.placed), 1)

        # second order, user declines in the prompt
        prev = await self.request(6, "tools/call", {"name": "preview_order", "arguments": {
            "symbol": "AAPL", "action": "SELL", "quantity": 5, "orderType": "LMT", "limitPrice": 101}})
        pid = json.loads(prev["result"]["content"][0]["text"].split("\n", 1)[1])["preview_id"]
        self.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                   "params": {"name": "place_order", "arguments": {"preview_id": pid, "confirm": True}}})
        ask = await self.recv()
        self.send({"jsonrpc": "2.0", "id": ask["id"], "result": {"action": "decline"}})
        done = await self.recv()
        self.assertTrue(done["result"]["isError"])
        self.assertEqual(len(self.app.fake.placed), 1)

    async def test_errors(self):
        self.server.handle_line(b"{not json")
        self.assertEqual((await self.recv())["error"]["code"], -32700)
        self.assertEqual((await self.request(1, "nope"))["error"]["code"], -32601)
        res = await self.request(2, "tools/call", {"name": "submit_live_order", "arguments": {}})
        self.assertEqual(res["error"]["code"], -32602)
        init = await self.request(3, "initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertFalse(self.server.elicitation_supported())


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SubprocessRoundTrip(unittest.TestCase):
    """The real entry point over real pipes. stdout must carry only JSON-RPC lines."""

    def run_server(self, env_extra, messages):
        env = {**os.environ, **env_extra, "CLAUDE_PLUGIN_DATA": os.path.join(
            os.environ.get("TMPDIR", "/tmp"), "ibkr-paper-gateway-test")}
        stdin = "".join(json.dumps(m) + "\n" for m in messages)
        proc = subprocess.run([sys.executable, MAIN], input=stdin.encode(), capture_output=True,
                              env=env, timeout=60)
        lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
        return [json.loads(ln) for ln in lines], proc.stderr.decode()

    def status_call(self, env):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "gateway_status", "arguments": {}}},
        ]
        out, err = self.run_server(env, msgs)
        by_id = {m["id"]: m for m in out}
        self.assertEqual(set(by_id), {1, 2, 3})
        text = by_id[3]["result"]["content"][0]["text"]
        return text, json.loads(text.split("\n", 1)[1]), err

    def test_nothing_listening(self):
        text, data, err = self.status_call({"IBKR_PAPER_PORT": str(free_port())})
        self.assertFalse(data["connected"])
        self.assertIn("Start IB Gateway", text)
        self.assertIn("ready", err)

    def test_live_port_refused(self):
        text, data, _ = self.status_call({"IBKR_PAPER_PORT": "4001"})
        self.assertFalse(data["endpoint_allowed"])
        self.assertIn("never connects to live ports", text)


if __name__ == "__main__":
    unittest.main()
