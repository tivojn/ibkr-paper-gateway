"""Checks the hooks against a real (unconnected) ib_async.IB object."""

import asyncio
import unittest

from ib_async import IB

from ibkr_paper.broker import Broker
from ibkr_paper.config import Config


class RealIBHooks(unittest.IsolatedAsyncioTestCase):
    async def test_managed_accounts_hook_resolves_waiters(self):
        ib = IB()
        broker = Broker(Config(), ib_factory=lambda: ib)
        broker._install_hooks(ib)
        fut = asyncio.get_running_loop().create_future()
        broker._acct_waiters.append(fut)
        # what the decoder does when message 15 (managedAccounts) arrives
        ib.client.decoder.handlers[15](["15", "1", "DU111,DU222,"])
        self.assertEqual(await asyncio.wait_for(fut, 1), ["DU111", "DU222"])
        self.assertEqual(ib.wrapper.accounts, ["DU111", "DU222"])

    async def test_error_and_disconnect_events(self):
        ib = IB()
        broker = Broker(Config(), ib_factory=lambda: ib)
        broker._install_hooks(ib)
        ib.errorEvent.emit(-1, 2104, "Market data farm connection is OK", None)
        self.assertEqual(broker.recent_errors[-1]["code"], 2104)
        ib.disconnectedEvent.emit()
        self.assertEqual(broker.disconnects, 1)

    async def test_live_port_never_connects_real_ib(self):
        cfg = Config(port=7496)
        made = []
        broker = Broker(cfg, ib_factory=lambda: made.append(1) or IB())
        with self.assertRaises(Exception) as cm:
            await broker.ensure()
        self.assertIn("LIVE", str(cm.exception))
        self.assertEqual(made, [])


if __name__ == "__main__":
    unittest.main()
