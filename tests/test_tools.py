import os
import stat
import unittest

from ib_async import Stock

from .fake_ib import FakeIB, make_position
from .helpers import WEEKEND_NOW, make_app, payload

BUY10 = {"symbol": "AAPL", "action": "BUY", "quantity": 10, "orderType": "LMT", "limitPrice": 100}


class Base(unittest.IsolatedAsyncioTestCase):
    def app(self, **kw):
        a = make_app(**kw)
        self.addCleanup(lambda: None)
        return a

    async def preview(self, app, args=None):
        res = await app.call("preview_order", dict(args or BUY10))
        self.assertFalse(res.get("isError"), res["content"][0]["text"])
        return payload(res)[1]["preview_id"]

    def assertRefused(self, res, needle=None):
        self.assertTrue(res.get("isError"), res["content"][0]["text"])
        if needle:
            self.assertIn(needle, res["content"][0]["text"])


class ConnectionTest(Base):
    async def test_live_port_never_dialled(self):
        app = self.app(port=4001)
        summary, data = payload(await app.call("gateway_status", {}))
        self.assertFalse(data["endpoint_allowed"])
        self.assertIn("LIVE", summary)
        self.assertRefused(await app.call("preview_order", BUY10), "LIVE")
        self.assertEqual(app.fake.connect_calls, [])

    async def test_non_loopback_never_dialled(self):
        app = self.app(host="192.168.1.20")
        self.assertRefused(await app.call("positions", {}), "loopback")
        self.assertEqual(app.fake.connect_calls, [])

    async def test_gateway_not_running(self):
        app = self.app(fake=FakeIB(refuse_connect=True))
        summary, data = payload(await app.call("gateway_status", {}))
        self.assertFalse(data["connected"])
        self.assertIn("Start IB Gateway", summary)
        self.assertRefused(await app.call("positions", {}), "PAPER login")

    async def test_connects_with_fixed_client_id_and_delayed_data(self):
        app = self.app()
        summary, data = payload(await app.call("gateway_status", {}))
        self.assertTrue(data["connected"])
        self.assertTrue(data["order_submission_allowed"])
        self.assertEqual(data["accounts"], [{"id": "DU1234567", "type": "paper"}])
        self.assertEqual(app.fake.connect_calls[0], ("127.0.0.1", 4002, 17, False))
        self.assertEqual(app.fake.market_data_type, 3)

    async def test_status_labels_live(self):
        app = self.app(fake=FakeIB(accounts=["DU1", "U2"]))
        _, data = payload(await app.call("gateway_status", {}))
        self.assertFalse(data["order_submission_allowed"])
        self.assertIn({"id": "U2", "type": "LIVE"}, data["accounts"])

    async def test_reconnect_after_drop(self):
        app = self.app()
        await app.call("positions", {})
        app.fake.drop()
        res = await app.call("positions", {})
        self.assertFalse(res.get("isError"))
        self.assertEqual(len(app.fake.connect_calls), 2)
        self.assertEqual(app.broker.disconnects, 1)


class ReadToolsTest(Base):
    async def test_reads(self):
        fake = FakeIB()
        fake.positions_list = [make_position("DU1234567", Stock("AAPL", "SMART", "USD"), 5, 99.0)]
        app = self.app(fake=fake)
        _, d = payload(await app.call("positions", {}))
        self.assertEqual(d["positions"][0]["position"], 5)
        s, d = payload(await app.call("portfolio", {}))
        self.assertIn("1 holding", s)
        s, d = payload(await app.call("account_summary", {}))
        self.assertIn("DU1234567", s)
        s, d = payload(await app.call("quote", {"symbol": "aapl"}))
        self.assertEqual(d["marketDataType"], "delayed")
        self.assertIn("Delayed", d["note"])
        s, d = payload(await app.call("history", {"symbol": "AAPL", "limit": 3}))
        self.assertEqual(len(d["bars"]), 3)
        s, d = payload(await app.call("open_orders", {}))
        self.assertEqual(d["orders"], [])
        s, d = payload(await app.call("executions", {"since": "2026-10-01T00:00:00Z"}))
        self.assertEqual(d["executions"], [])
        self.assertRefused(await app.call("executions", {"since": "yesterday"}))
        self.assertRefused(await app.call("quote", {"symbol": "NOPE"}), "could not find")


class PreviewPlaceTest(Base):
    async def test_happy_path(self):
        app = self.app()
        res = await app.call("preview_order", BUY10)
        summary, data = payload(res)
        self.assertIn("PREVIEW (not sent)", summary)
        self.assertEqual(data["estimatedNotional"], 1000.0)
        self.assertEqual(data["estimatedCommission"], 1.0)
        self.assertEqual(app.fake.placed, [])
        summary, data = payload(await app.call("place_order", {"preview_id": data["preview_id"], "confirm": True}))
        self.assertIn("PAPER order", summary)
        self.assertEqual(len(app.fake.placed), 1)
        contract, order = app.fake.placed[0]
        self.assertEqual((order.action, order.totalQuantity, order.orderType, order.lmtPrice, order.account),
                         ("BUY", 10.0, "LMT", 100.0, "DU1234567"))

    async def test_confirm_required(self):
        app = self.app()
        pid = await self.preview(app)
        self.assertRefused(await app.call("place_order", {"preview_id": pid}), "confirm")
        self.assertRefused(await app.call("place_order", {"preview_id": pid, "confirm": "true"}), "confirm")
        self.assertRefused(await app.call("place_order", {"preview_id": pid, "confirm": 1}), "confirm")
        self.assertEqual(app.fake.placed, [])

    async def test_unknown_expired_and_reused_preview(self):
        app = self.app()
        self.assertRefused(await app.call("place_order", {"preview_id": "pv_nope", "confirm": True}), "unknown")
        self.assertRefused(await app.call("place_order", {"confirm": True}), "preview_id")
        pid = await self.preview(app)
        app.test_clock.advance(121)
        self.assertRefused(await app.call("place_order", {"preview_id": pid, "confirm": True}), "expired")
        pid = await self.preview(app)
        app.test_clock.advance(119)
        self.assertFalse((await app.call("place_order", {"preview_id": pid, "confirm": True})).get("isError"))
        self.assertRefused(await app.call("place_order", {"preview_id": pid, "confirm": True}), "already used")
        self.assertEqual(len(app.fake.placed), 1)

    async def test_gate_rechecked_fresh_on_place(self):
        app = self.app()
        pid = await self.preview(app)
        before = app.fake.client.managed_requests
        app.fake.accounts = ["DU1234567", "U7654321"]  # a live account appears after the preview
        self.assertRefused(await app.call("place_order", {"preview_id": pid, "confirm": True}), "LIVE")
        self.assertGreater(app.fake.client.managed_requests, before)
        self.assertEqual(app.fake.placed, [])

    async def test_gate_cases_on_preview(self):
        for accounts, args, needle in [
            (["U1234567"], BUY10, "LIVE"),
            (["DU1", "U2"], {**BUY10, "account": "DU1"}, "LIVE"),
            ([], BUY10, "no managed accounts"),
            (["DU1"], {**BUY10, "account": "DU2"}, "not one of"),
            (["DU1", "DU2"], BUY10, "pass `account`"),
        ]:
            app = self.app(fake=FakeIB(accounts=accounts))
            self.assertRefused(await app.call("preview_order", args), needle)

    async def test_gateway_silent_on_accounts(self):
        fake = FakeIB()
        fake.answer_managed = False
        app = self.app(fake=fake, request_timeout=0.2)
        self.assertRefused(await app.call("preview_order", BUY10), "managed-accounts")

    async def test_limits(self):
        app = self.app()
        self.assertRefused(await app.call("preview_order", {**BUY10, "quantity": 101}), "per-order limit")
        self.assertRefused(await app.call("preview_order", {**BUY10, "quantity": 100, "limitPrice": 600}), "notional")
        self.assertRefused(await app.call("preview_order", {**BUY10, "quantity": 1.5}), "whole number")
        self.assertRefused(await app.call("preview_order", {**BUY10, "currency": "EUR"}), "USD")
        self.assertRefused(await app.call("preview_order", {
            **BUY10, "secType": "OPT", "expiry": "20261120", "strike": 200, "right": "C"}), "OPT")
        self.assertRefused(await app.call("preview_order", {**BUY10, "orderType": "LMT", "limitPrice": None}))
        self.assertRefused(await app.call("preview_order", {**BUY10, "action": "SHORT"}), "BUY or SELL")

    async def test_sell_notional_uses_market_price(self):
        # a SELL limit far below market would fill near market: notional uses the higher price
        app = self.app(fake=FakeIB(price=6000.0))
        self.assertRefused(await app.call("preview_order", {**BUY10, "action": "SELL", "limitPrice": 1}), "notional")

    async def test_options_opt_in(self):
        from ibkr_paper.config import Config
        app = self.app(sec_types=Config.from_env({"IBKR_PAPER_ALLOW_OPT": "1"}).sec_types, )
        app.fake.price = 2.0
        res = await app.call("preview_order", {"symbol": "AAPL", "secType": "OPT", "expiry": "20261120",
                                               "strike": 200, "right": "C", "action": "BUY",
                                               "quantity": 2, "orderType": "LMT", "limitPrice": 2.5})
        _, d = payload(res)
        self.assertEqual(d["estimatedNotional"], 500.0)
        self.assertRefused(await app.call("preview_order", {"symbol": "AAPL", "secType": "OPT",
                                                            "expiry": "20261120", "strike": 200, "right": "C",
                                                            "action": "BUY", "quantity": 11,
                                                            "orderType": "LMT", "limitPrice": 2.5}), "contracts")

    async def test_market_order_hours(self):
        mkt = {"symbol": "AAPL", "action": "BUY", "quantity": 10, "orderType": "MKT"}
        app = self.app(now=WEEKEND_NOW)
        self.assertRefused(await app.call("preview_order", mkt), "regular trading hours")
        self.assertFalse((await app.call("preview_order", {**mkt, "orderType": "LMT", "limitPrice": 100,
                                                            "outsideRth": True})).get("isError"))
        app = self.app()
        self.assertRefused(await app.call("preview_order", {**mkt, "outsideRth": True}), "outsideRth")
        self.assertFalse((await app.call("preview_order", mkt)).get("isError"))
        app = self.app(now=WEEKEND_NOW, allow_mkt_outside_rth=True)
        self.assertFalse((await app.call("preview_order", mkt)).get("isError"))

    async def test_market_order_without_price_refused(self):
        app = self.app(fake=FakeIB(price=None))
        self.assertRefused(await app.call("preview_order", {"symbol": "AAPL", "action": "BUY", "quantity": 1,
                                                            "orderType": "MKT"}), "no price")

    async def test_whatif_rejection_refused(self):
        app = self.app()
        app.fake.whatif_error = "Order rejected - reason: insufficient"
        self.assertRefused(await app.call("preview_order", BUY10), "what-if")

    async def test_rate_limit(self):
        app = self.app(max_orders_per_min=2)
        from ibkr_paper.policy import RateLimiter
        app.rate = RateLimiter(2, app.test_clock)
        for _ in range(2):
            pid = await self.preview(app)
            self.assertFalse((await app.call("place_order", {"preview_id": pid, "confirm": True})).get("isError"))
        pid = await self.preview(app)
        self.assertRefused(await app.call("place_order", {"preview_id": pid, "confirm": True}), "rate limit")
        app.test_clock.advance(61)
        pid = await self.preview(app)
        self.assertFalse((await app.call("place_order", {"preview_id": pid, "confirm": True})).get("isError"))


class ModifyCancelTest(Base):
    async def place(self, app):
        pid = await self.preview(app)
        _, d = payload(await app.call("place_order", {"preview_id": pid, "confirm": True}))
        return d["orderId"]

    async def test_cancel(self):
        app = self.app()
        oid = await self.place(app)
        self.assertRefused(await app.call("cancel_order", {"orderId": oid}), "confirm")
        self.assertRefused(await app.call("cancel_order", {"orderId": 999, "confirm": True}), "not an open order")
        app.fake.accounts = ["U1"]
        self.assertRefused(await app.call("cancel_order", {"orderId": oid, "confirm": True}), "LIVE")
        self.assertEqual(app.fake.cancelled, [])
        app.fake.accounts = ["DU1234567"]
        summary, _ = payload(await app.call("cancel_order", {"orderId": oid, "confirm": True}))
        self.assertIn("Cancel sent", summary)
        self.assertEqual(app.fake.cancelled, [oid])

    async def test_modify_two_step(self):
        app = self.app()
        oid = await self.place(app)
        summary, d = payload(await app.call("modify_order", {"orderId": oid, "limitPrice": 99.5}))
        self.assertIn("MODIFY PREVIEW", summary)
        self.assertEqual(len(app.fake.placed), 1)  # nothing sent yet
        pid = d["preview_id"]
        self.assertRefused(await app.call("modify_order", {"orderId": oid, "preview_id": pid}), "confirm")
        self.assertRefused(await app.call("modify_order", {"orderId": oid + 1, "preview_id": pid, "confirm": True}),
                           "preview is for order")
        res = await app.call("modify_order", {"orderId": oid, "preview_id": pid, "confirm": True})
        self.assertFalse(res.get("isError"), res)
        self.assertEqual(len(app.fake.placed), 2)
        self.assertEqual(app.fake.placed[1][1].orderId, oid)
        self.assertEqual(app.fake.placed[1][1].lmtPrice, 99.5)

    async def test_modify_gated_and_limited(self):
        app = self.app()
        oid = await self.place(app)
        self.assertRefused(await app.call("modify_order", {"orderId": oid, "quantity": 500}), "per-order limit")
        self.assertRefused(await app.call("modify_order", {"orderId": oid}), "at least one change")
        _, d = payload(await app.call("modify_order", {"orderId": oid, "quantity": 20}))
        app.fake.accounts = ["DU1234567", "U9"]
        self.assertRefused(await app.call("modify_order", {"orderId": oid, "preview_id": d["preview_id"],
                                                           "confirm": True}), "LIVE")
        self.assertEqual(len(app.fake.placed), 1)
        # a place preview cannot be used to modify
        app.fake.accounts = ["DU1234567"]
        pid = await self.preview(app)
        self.assertRefused(await app.call("modify_order", {"orderId": oid, "preview_id": pid, "confirm": True}),
                           "not modify")


class AuditTest(Base):
    async def test_entries_and_mode(self):
        app = self.app()
        self.assertRefused(await app.call("place_order", {"preview_id": "x", "confirm": True}))
        pid = await self.preview(app)
        _, d = payload(await app.call("place_order", {"preview_id": pid, "confirm": True}))
        trade = app.fake.trades[0]
        app.fake.fill(trade, 10, 100.0)
        mode = stat.S_IMODE(os.stat(app.audit.path).st_mode)
        self.assertEqual(mode, 0o600)
        _, log = payload(await app.call("paper_log", {"limit": 50}))
        events = [(e["event"], e["decision"]) if "decision" in e else (e["event"], None) for e in log["entries"]]
        self.assertEqual(events, [("place", "refused"), ("preview", "allowed"), ("place", "submitting"),
                                  ("place", "placed"), ("fill", None)])
        placed = log["entries"][3]
        self.assertEqual(placed["orderId"], d["orderId"])
        self.assertEqual(placed["accounts_seen"], ["DU1234567"])
        self.assertEqual(log["entries"][4]["fill"]["price"], 100.0)
        self.assertIn("unknown", log["entries"][0]["reason"])

    async def test_audit_failure_blocks_order(self):
        app = self.app()
        pid = await self.preview(app)
        app.audit.append = lambda entry: False
        self.assertRefused(await app.call("place_order", {"preview_id": pid, "confirm": True}), "audit log")
        self.assertEqual(app.fake.placed, [])


class ElicitationTest(Base):
    def wire(self, app, decision, supported=True):
        calls = []

        async def elicit(message, schema):
            calls.append((message, schema))
            if decision is None:
                return {"action": "cancel"}
            return {"action": "accept", "content": {"decision": decision}}

        app.elicit = elicit
        app.elicitation_supported = lambda: supported
        return calls

    async def test_approve(self):
        app = self.app()
        calls = self.wire(app, "Approve")
        pid = await self.preview(app)
        self.assertFalse((await app.call("place_order", {"preview_id": pid, "confirm": True})).get("isError"))
        self.assertEqual(len(calls), 1)
        self.assertIn("BUY 10", calls[0][0])
        self.assertEqual(calls[0][1]["properties"]["decision"]["enum"], ["Approve", "Cancel"])

    async def test_cancel_choice_and_dismiss(self):
        for decision in ("Cancel", None):
            app = self.app()
            self.wire(app, decision)
            pid = await self.preview(app)
            self.assertRefused(await app.call("place_order", {"preview_id": pid, "confirm": True}), "Cancelled")
            self.assertEqual(app.fake.placed, [])
            _, log = payload(await app.call("paper_log", {}))
            self.assertEqual(log["entries"][-1]["decision"], "refused")

    async def test_auto_mode_skips_prompt_but_needs_preview(self):
        app = self.app(auto=True)
        calls = self.wire(app, "Cancel")
        self.assertRefused(await app.call("place_order", {"preview_id": "pv_x", "confirm": True}), "unknown")
        pid = await self.preview(app)
        self.assertFalse((await app.call("place_order", {"preview_id": pid, "confirm": True})).get("isError"))
        self.assertEqual(calls, [])

    async def test_unsupported_client_no_prompt(self):
        app = self.app()
        calls = self.wire(app, "Cancel", supported=False)
        pid = await self.preview(app)
        self.assertFalse((await app.call("place_order", {"preview_id": pid, "confirm": True})).get("isError"))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
