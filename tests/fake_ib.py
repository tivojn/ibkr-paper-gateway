"""An offline stand-in for ib_async.IB with just the surface the server uses."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from ib_async import (
    AccountValue, CommissionReport, Execution, Fill, OrderState, OrderStatus, PortfolioItem,
    Position, Ticker, Trade,
)
from ib_async.objects import BarData


class Hook:
    """Minimal eventkit-like event: supports += and emit."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def emit(self, *args):
        for h in self.handlers:
            h(*args)


class FakeWrapper:
    def __init__(self):
        self.accounts = []

    def managedAccounts(self, accounts_list: str):
        self.accounts = [a for a in accounts_list.split(",") if a]


class FakeClient:
    def __init__(self, ib):
        self.ib = ib
        self.managed_requests = 0

    def reqManagedAccts(self):
        self.managed_requests += 1
        accounts = ",".join(self.ib.accounts)
        if self.ib.answer_managed:
            asyncio.get_running_loop().call_soon(self.ib.wrapper.managedAccounts, accounts)


class FakeIB:
    def __init__(self, accounts=("DU1234567",), price=100.0, refuse_connect=False):
        self.accounts = list(accounts)
        self.price = price
        self.refuse_connect = refuse_connect
        self.answer_managed = True
        self.connected = False
        self.connect_calls = []
        self.wrapper = FakeWrapper()
        self.client = FakeClient(self)
        self.errorEvent = Hook()
        self.disconnectedEvent = Hook()
        self.placed = []
        self.cancelled = []
        self.trades = []
        self.next_order_id = 1000
        self.whatif_error = None
        self.market_data_type = None
        self.positions_list = []
        self.fills_list = []

    # connection
    async def connectAsync(self, host, port, clientId=1, timeout=4, readonly=False, **kw):
        self.connect_calls.append((host, port, clientId, readonly))
        if self.refuse_connect:
            raise ConnectionRefusedError(61, "Connect call failed")
        self.connected = True
        self.wrapper.accounts = list(self.accounts)

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    def drop(self):
        self.connected = False
        self.disconnectedEvent.emit()

    def managedAccounts(self):
        return list(self.wrapper.accounts)

    def reqMarketDataType(self, t):
        self.market_data_type = t

    # data
    async def reqCurrentTimeAsync(self):
        return datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)

    async def qualifyContractsAsync(self, *contracts, **kw):
        out = []
        for c in contracts:
            if c.symbol == "NOPE":
                out.append(None)
                continue
            c.conId = 265598
            if c.secType == "STK" and not c.primaryExchange:
                c.primaryExchange = "NASDAQ" if c.currency == "USD" else "LSE"
            out.append(c)
        return out

    async def reqContractDetailsAsync(self, contract):
        return []

    async def reqTickersAsync(self, *contracts, **kw):
        out = []
        for c in contracts:
            t = Ticker(contract=c)
            if self.price is not None:
                t.bid, t.ask, t.last, t.close = self.price - 0.05, self.price + 0.05, self.price, self.price
            t.marketDataType = 3
            out.append(t)
        return out

    async def whatIfOrderAsync(self, contract, order):
        if self.whatif_error:
            raise RuntimeError(self.whatif_error)
        return OrderState(status="PreSubmitted", commission=1.0, commissionCurrency="USD",
                          initMarginChange="250.0", maintMarginChange="200.0")

    async def accountSummaryAsync(self, account=""):
        return [AccountValue(a, "NetLiquidation", "1000000.00", "USD", "") for a in self.accounts]

    async def reqPositionsAsync(self):
        return list(self.positions_list)

    def portfolio(self, account=""):
        return [
            PortfolioItem(p.contract, p.position, 101.0, 101.0 * p.position, p.avgCost, 10.0, 0.0, p.account)
            for p in self.positions_list
            if not account or p.account == account
        ]

    async def reqHistoricalDataAsync(self, contract, end, duration, bar, what, use_rth, **kw):
        return [BarData(date=datetime(2026, 10, 6, 14, i, tzinfo=timezone.utc), open=1, high=2,
                        low=0.5, close=1.5, volume=100) for i in range(5)]

    async def reqExecutionsAsync(self, flt=None):
        return list(self.fills_list)

    # orders
    def placeOrder(self, contract, order):
        if not order.orderId:
            self.next_order_id += 1
            order.orderId = self.next_order_id
            order.clientId = 17
        self.placed.append((contract, order))
        existing = next((t for t in self.trades if t.order.orderId == order.orderId), None)
        if existing:
            existing.order = order
            return existing
        trade = Trade(contract=contract, order=order,
                      orderStatus=OrderStatus(orderId=order.orderId, status="Submitted",
                                              remaining=order.totalQuantity))
        self.trades.append(trade)
        return trade

    def cancelOrder(self, order, manualCancelOrderTime=""):
        self.cancelled.append(order.orderId)
        for t in self.trades:
            if t.order.orderId == order.orderId:
                t.orderStatus.status = "Cancelled"
                return t
        return None

    async def reqOpenOrdersAsync(self):
        return self.openTrades()

    async def reqAllOpenOrdersAsync(self):
        return self.openTrades()

    def openTrades(self):
        return [t for t in self.trades if t.orderStatus.status not in ("Cancelled", "Filled")]

    def fill(self, trade, shares, price):
        ex = Execution(execId=f"ex{len(self.fills_list)}", time=datetime.now(timezone.utc),
                       acctNumber=trade.order.account, side="BOT", shares=shares, price=price,
                       orderId=trade.order.orderId)
        f = Fill(trade.contract, ex, CommissionReport(commission=1.0), ex.time)
        self.fills_list.append(f)
        trade.fills.append(f)
        trade.fillEvent.emit(trade, f)
        return f


def make_position(account, contract, qty, avg):
    return Position(account, contract, qty, avg)
