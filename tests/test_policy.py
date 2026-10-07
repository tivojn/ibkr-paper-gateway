import unittest
from types import SimpleNamespace

from ibkr_paper import policy
from ibkr_paper.config import Config
from ibkr_paper.policy import PolicyError

from .helpers import RTH_NOW, WEEKEND_NOW, Clock


class PortPolicyTest(unittest.TestCase):
    def test_live_ports_refused(self):
        for port in (4001, 7496):
            with self.assertRaises(PolicyError) as cm:
                policy.check_endpoint("127.0.0.1", port)
            self.assertIn("LIVE", str(cm.exception))

    def test_paper_ports_ok(self):
        self.assertEqual(policy.check_endpoint("127.0.0.1", 4002), "IB Gateway paper")
        self.assertEqual(policy.check_endpoint("127.0.0.1", 7497), "TWS paper")

    def test_custom_port_allowed_but_labelled(self):
        self.assertIn("custom", policy.check_endpoint("127.0.0.1", 4102))

    def test_non_loopback_refused(self):
        for host in ("192.168.1.10", "10.0.0.2", "example.com", "0.0.0.0", "8.8.8.8"):
            with self.assertRaises(PolicyError):
                policy.check_endpoint(host, 4002)

    def test_loopback_variants_ok(self):
        for host in ("127.0.0.1", "localhost", "::1", "127.0.0.2"):
            policy.check_endpoint(host, 4002)
        self.assertEqual(policy.connect_host("localhost"), "127.0.0.1")

    def test_invalid_port(self):
        with self.assertRaises(PolicyError):
            policy.check_endpoint("127.0.0.1", -1)

    def test_env_port_parsing(self):
        cfg = Config.from_env({"IBKR_PAPER_PORT": "4001"})
        with self.assertRaises(PolicyError):
            policy.check_endpoint(cfg.host, cfg.port)
        cfg = Config.from_env({"IBKR_PAPER_PORT": "abc"})
        with self.assertRaises(PolicyError):
            policy.check_endpoint(cfg.host, cfg.port)
        cfg = Config.from_env({})
        self.assertEqual((cfg.host, cfg.port, cfg.client_id), ("127.0.0.1", 4002, 17))


class PaperGateTest(unittest.TestCase):
    def test_du_ok(self):
        self.assertEqual(policy.paper_gate(["DU123"]), "DU123")
        self.assertEqual(policy.paper_gate(["DU123", "DF9"], "DF9"), "DF9")

    def test_live_refused(self):
        with self.assertRaises(PolicyError):
            policy.paper_gate(["U1234567"])

    def test_mixed_refused(self):
        with self.assertRaises(PolicyError) as cm:
            policy.paper_gate(["DU123", "U1234567"], "DU123")
        self.assertIn("U1234567", str(cm.exception))

    def test_empty_refused(self):
        for accts in ([], None, [""]):
            with self.assertRaises(PolicyError):
                policy.paper_gate(accts)

    def test_target_not_in_list_refused(self):
        with self.assertRaises(PolicyError):
            policy.paper_gate(["DU123"], "DU999")
        with self.assertRaises(PolicyError):
            policy.paper_gate(["DU123"], "U123")

    def test_several_need_target(self):
        with self.assertRaises(PolicyError):
            policy.paper_gate(["DU1", "DU2"])

    def test_lowercase_and_other_prefixes_not_paper(self):
        for a in ("du123", "F123", "I123", "DX1"):
            self.assertFalse(policy.is_paper_account(a))


class LimitsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_quantity(self):
        policy.check_quantity(self.cfg, "STK", 100)
        with self.assertRaises(PolicyError):
            policy.check_quantity(self.cfg, "STK", 101)
        policy.check_quantity(self.cfg, "OPT", 10)
        with self.assertRaises(PolicyError):
            policy.check_quantity(self.cfg, "OPT", 11)

    def test_notional(self):
        policy.check_notional(self.cfg, 50_000)
        with self.assertRaises(PolicyError):
            policy.check_notional(self.cfg, 50_000.01)

    def test_sec_types(self):
        policy.check_sec_type(self.cfg, "STK")
        policy.check_sec_type(self.cfg, "ETF")
        with self.assertRaises(PolicyError):
            policy.check_sec_type(self.cfg, "OPT")
        cfg = Config.from_env({"IBKR_PAPER_ALLOW_OPT": "1"})
        policy.check_sec_type(cfg, "OPT")
        # OPT in the list alone does not enable options
        cfg = Config.from_env({"IBKR_PAPER_SEC_TYPES": "STK,OPT"})
        with self.assertRaises(PolicyError):
            policy.check_sec_type(cfg, "OPT")

    def test_us_only(self):
        policy.check_us(self.cfg, SimpleNamespace(currency="USD", primaryExchange="NASDAQ", secType="STK", symbol="AAPL"))
        with self.assertRaises(PolicyError):
            policy.check_us(self.cfg, SimpleNamespace(currency="EUR", primaryExchange="IBIS", secType="STK", symbol="SAP"))
        with self.assertRaises(PolicyError):
            policy.check_us(self.cfg, SimpleNamespace(currency="USD", primaryExchange="PINK", secType="STK", symbol="X"))

    def test_env_invalid_limits_fall_back(self):
        cfg = Config.from_env({"IBKR_PAPER_MAX_QTY": "-5", "IBKR_PAPER_MAX_NOTIONAL": "lots",
                               "IBKR_PAPER_MAX_ORDERS_PER_MIN": "0"})
        self.assertEqual((cfg.max_qty_stk, cfg.max_notional, cfg.max_orders_per_min), (100, 50_000.0, 10))
        cfg = Config.from_env({"IBKR_PAPER_MAX_QTY": "5", "IBKR_PAPER_AUTO": "1"})
        self.assertEqual(cfg.max_qty_stk, 5.0)
        self.assertTrue(cfg.auto)

    def test_rate_limiter(self):
        clock = Clock()
        rl = policy.RateLimiter(2, clock)
        rl.check(); rl.record()
        rl.check(); rl.record()
        with self.assertRaises(PolicyError):
            rl.check()
        clock.advance(60)
        rl.check()

    def test_hours(self):
        self.assertTrue(policy.is_regular_hours(RTH_NOW))
        self.assertFalse(policy.is_regular_hours(WEEKEND_NOW))
        policy.check_market_hours(self.cfg, "LMT", True, False)  # limit orders are fine
        with self.assertRaises(PolicyError):
            policy.check_market_hours(self.cfg, "MKT", False, False)
        with self.assertRaises(PolicyError):
            policy.check_market_hours(self.cfg, "MKT", True, True)
        self.cfg.allow_mkt_outside_rth = True
        policy.check_market_hours(self.cfg, "MKT", True, False)


if __name__ == "__main__":
    unittest.main()
