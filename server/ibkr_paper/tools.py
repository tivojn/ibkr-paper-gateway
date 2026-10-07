"""MCP tools: definitions and handlers.

Order-affecting tools (place_order, modify_order, cancel_order) run the fresh
paper gate on every call. place_order and modify_order need a preview_id from
a server-side read-back made in the last 2 minutes, plus confirm: true.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import secrets
import time
from datetime import datetime, timezone

from ib_async import Contract, ExecutionFilter, Index, Option, Order, Stock

from . import policy
from .audit import AuditLog
from .broker import Broker, GatewayUnavailable
from .policy import PolicyError

ORDER_TYPES = {"MKT": "MKT", "LMT": "LMT", "STP": "STP", "STP_LMT": "STP LMT"}
IB_TO_ORDER_TYPE = {v: k for k, v in ORDER_TYPES.items()}
DATA_SEC_TYPES = {"STK", "ETF", "OPT", "IND"}
MD_TYPES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9 .\-/]{0,15}$")
SUMMARY_TAGS = [
    "NetLiquidation", "TotalCashValue", "BuyingPower", "AvailableFunds", "ExcessLiquidity",
    "EquityWithLoanValue", "GrossPositionValue", "InitMarginReq", "MaintMarginReq",
    "UnrealizedPnL", "RealizedPnL", "SettledCash", "AccountType", "Cushion",
]
HELP_SUFFIX = " Paper accounts only (DU…/DF…)."


class ToolError(Exception):
    """A non-policy failure reported to the model as an error result."""


# -- small helpers ---------------------------------------------------------
def num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or abs(f) >= 1e300:
        return None
    return round(f, 6)


def money(x) -> str:
    return "?" if x is None else f"${x:,.2f}"


def result(summary: str, data, is_error: bool = False) -> dict:
    text = summary + "\n" + json.dumps(data, default=str, separators=(",", ":"))
    out = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out


def contract_dict(c) -> dict:
    d = {
        "symbol": c.symbol,
        "secType": c.secType,
        "exchange": c.exchange or None,
        "primaryExchange": getattr(c, "primaryExchange", "") or None,
        "currency": c.currency,
        "conId": c.conId or None,
    }
    if c.secType == "OPT":
        d.update(
            expiry=c.lastTradeDateOrContractMonth,
            strike=num(c.strike),
            right=c.right,
            multiplier=c.multiplier,
        )
    return {k: v for k, v in d.items() if v is not None}


def _str_arg(args, key, default=None):
    v = args.get(key, default)
    return v.strip() if isinstance(v, str) else v


def build_contract(args) -> tuple:
    """Return (contract, requested secType label)."""
    symbol = _str_arg(args, "symbol")
    if not isinstance(symbol, str) or not symbol:
        raise ToolError("`symbol` is required.")
    symbol = symbol.upper()
    if not SYMBOL_RE.match(symbol):
        raise ToolError(f"`symbol` {symbol!r} does not look like a ticker.")
    sec = (_str_arg(args, "secType") or "STK").upper()
    if sec not in DATA_SEC_TYPES:
        raise ToolError(f"secType {sec} is not supported (use STK, ETF, OPT or IND).")
    exchange = (_str_arg(args, "exchange") or ("CBOE" if sec == "IND" else "SMART")).upper()
    currency = (_str_arg(args, "currency") or "USD").upper()
    if sec in ("STK", "ETF"):
        c = Stock(symbol, exchange, currency)
        primary = _str_arg(args, "primaryExchange")
        if primary:
            c.primaryExchange = primary.upper()
    elif sec == "IND":
        c = Index(symbol, exchange, currency)
    else:
        expiry = str(_str_arg(args, "expiry") or "")
        strike = num(args.get("strike"))
        right = (_str_arg(args, "right") or "").upper()[:1]
        if not re.fullmatch(r"\d{6}(\d{2})?", expiry) or strike is None or right not in ("C", "P"):
            raise ToolError("Options need `expiry` (YYYYMMDD), `strike` and `right` (C or P).")
        c = Option(symbol, expiry, strike, right, exchange, multiplier="100", currency=currency)
    return c, sec


def parse_quantity(v) -> float:
    q = num(v)
    if q is None or q <= 0:
        raise ToolError("`quantity` must be a positive number.")
    if q != int(q):
        raise ToolError("`quantity` must be a whole number.")
    return float(int(q))


def parse_price(args, key, required: bool):
    v = args.get(key)
    if v is None:
        if required:
            raise ToolError(f"`{key}` is required for this order type.")
        return None
    p = num(v)
    if p is None or p <= 0:
        raise ToolError(f"`{key}` must be a positive number.")
    return p


def parse_order_args(args) -> dict:
    action = (_str_arg(args, "action") or "").upper()
    if action not in ("BUY", "SELL"):
        raise ToolError("`action` must be BUY or SELL.")
    otype = (_str_arg(args, "orderType") or "").upper().replace(" ", "_")
    if otype not in ORDER_TYPES:
        raise ToolError("`orderType` must be MKT, LMT, STP or STP_LMT.")
    limit = parse_price(args, "limitPrice", otype in ("LMT", "STP_LMT"))
    stop = parse_price(args, "stopPrice", otype in ("STP", "STP_LMT"))
    if otype in ("MKT", "STP") and limit is not None:
        raise ToolError(f"`limitPrice` does not apply to {otype} orders.")
    if otype in ("MKT", "LMT") and stop is not None:
        raise ToolError(f"`stopPrice` does not apply to {otype} orders.")
    tif = (_str_arg(args, "tif") or "DAY").upper()
    if tif not in ("DAY", "GTC"):
        raise ToolError("`tif` must be DAY or GTC.")
    outside = args.get("outsideRth", False)
    if not isinstance(outside, bool):
        raise ToolError("`outsideRth` must be a boolean.")
    return {
        "action": action,
        "quantity": parse_quantity(args.get("quantity")),
        "orderType": otype,
        "limitPrice": limit,
        "stopPrice": stop,
        "tif": tif,
        "outsideRth": outside,
    }


def make_order(p: dict, account: str) -> Order:
    o = Order(
        action=p["action"],
        totalQuantity=p["quantity"],
        orderType=ORDER_TYPES[p["orderType"]],
        tif=p["tif"],
        outsideRth=p["outsideRth"],
        account=account,
        transmit=True,
        orderRef="ibkr-paper-gateway",
    )
    if p["limitPrice"] is not None:
        o.lmtPrice = p["limitPrice"]
    if p["stopPrice"] is not None:
        o.auxPrice = p["stopPrice"]
    return o


def order_params_from(order) -> dict:
    otype = IB_TO_ORDER_TYPE.get(order.orderType, order.orderType)
    return {
        "action": order.action,
        "quantity": float(order.totalQuantity),
        "orderType": otype,
        "limitPrice": num(order.lmtPrice) if otype in ("LMT", "STP_LMT") else None,
        "stopPrice": num(order.auxPrice) if otype in ("STP", "STP_LMT") else None,
        "tif": order.tif or "DAY",
        "outsideRth": bool(order.outsideRth),
    }


def readback(p: dict, contract, account: str) -> str:
    price = ""
    if p["orderType"] == "LMT":
        price = f" limit {p['limitPrice']:g}"
    elif p["orderType"] == "STP":
        price = f" stop {p['stopPrice']:g}"
    elif p["orderType"] == "STP_LMT":
        price = f" stop {p['stopPrice']:g} limit {p['limitPrice']:g}"
    kind = {"MKT": "market", "LMT": "limit", "STP": "stop", "STP_LMT": "stop-limit"}[p["orderType"]]
    what = contract.symbol
    if contract.secType == "OPT":
        what = (
            f"{contract.symbol} {contract.lastTradeDateOrContractMonth} "
            f"{contract.strike:g}{contract.right} option"
        )
    unit = "contract(s)" if contract.secType == "OPT" else "share(s)"
    rth = ", outside RTH allowed" if p["outsideRth"] else ""
    return (
        f"{p['action']} {p['quantity']:g} {unit} of {what} — {kind} order{price}, "
        f"{p['tif']}{rth}, PAPER account {account}"
    )


def trade_dict(t) -> dict:
    o, s = t.order, t.orderStatus
    p = order_params_from(o)
    return {
        "orderId": o.orderId,
        "permId": o.permId or None,
        "clientId": o.clientId,
        "account": o.account,
        "contract": contract_dict(t.contract),
        **p,
        "status": s.status,
        "filled": num(s.filled),
        "remaining": num(s.remaining),
        "avgFillPrice": num(s.avgFillPrice),
    }


def fill_dict(f) -> dict:
    e = f.execution
    cr = f.commissionReport
    return {
        "execId": e.execId,
        "time": str(f.time),
        "account": e.acctNumber,
        "symbol": f.contract.symbol,
        "secType": f.contract.secType,
        "side": e.side,
        "shares": num(e.shares),
        "price": num(e.price),
        "orderId": e.orderId,
        "permId": e.permId,
        "commission": num(getattr(cr, "commission", None)) if cr else None,
        "realizedPNL": num(getattr(cr, "realizedPNL", None)) if cr else None,
    }


def ticker_price(t):
    """Best reference price from a ticker: market price, then last, then close."""
    if t is None:
        return None
    for fn in ("marketPrice",):
        try:
            p = num(getattr(t, fn)())
            if p and p > 0:
                return p
        except Exception:
            pass
    for attr in ("last", "close"):
        p = num(getattr(t, attr, None))
        if p and p > 0:
            return p
    return None


# -- tool definitions ------------------------------------------------------
def _schema(props: dict, required=()) -> dict:
    s = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        s["required"] = list(required)
    return s


ACCOUNT_PROP = {"type": "string", "description": "Paper account id (DU…). Optional when there is one."}
CONTRACT_PROPS = {
    "symbol": {"type": "string", "description": "Ticker, e.g. AAPL"},
    "secType": {"type": "string", "enum": ["STK", "ETF", "OPT", "IND"], "default": "STK"},
    "exchange": {"type": "string", "description": "Default SMART (CBOE for IND)"},
    "primaryExchange": {"type": "string", "description": "Optional, to disambiguate (e.g. NASDAQ)"},
    "currency": {"type": "string", "default": "USD"},
    "expiry": {"type": "string", "description": "OPT only: YYYYMMDD"},
    "strike": {"type": "number", "description": "OPT only"},
    "right": {"type": "string", "enum": ["C", "P"], "description": "OPT only"},
}
ORDER_PROPS = {
    "account": ACCOUNT_PROP,
    **CONTRACT_PROPS,
    "action": {"type": "string", "enum": ["BUY", "SELL"]},
    "quantity": {"type": "number", "description": "Shares or option contracts (whole number)"},
    "orderType": {"type": "string", "enum": ["MKT", "LMT", "STP", "STP_LMT"]},
    "limitPrice": {"type": "number"},
    "stopPrice": {"type": "number"},
    "tif": {"type": "string", "enum": ["DAY", "GTC"], "default": "DAY"},
    "outsideRth": {"type": "boolean", "default": False},
}
READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}

TOOLS = [
    {
        "name": "gateway_status",
        "description": "IB Gateway connection status: host/port (paper or live port), connected, "
        "managed accounts labelled paper/LIVE, server time, limits, and whether paper order "
        "submission is allowed right now and why.",
        "inputSchema": _schema({}),
        "annotations": READ,
    },
    {
        "name": "account_summary",
        "description": "Account balances: net liquidation, cash, buying power, margin, P&L.",
        "inputSchema": _schema({"account": ACCOUNT_PROP}),
        "annotations": READ,
    },
    {
        "name": "positions",
        "description": "Positions (quantity and average cost) per account.",
        "inputSchema": _schema({"account": ACCOUNT_PROP}),
        "annotations": READ,
    },
    {
        "name": "portfolio",
        "description": "Positions with market price, market value and unrealized/realized P&L.",
        "inputSchema": _schema({"account": ACCOUNT_PROP}),
        "annotations": READ,
    },
    {
        "name": "quote",
        "description": "Snapshot quote (bid/ask/last/close). Uses delayed data when there is no "
        "live market-data subscription and says which one you got.",
        "inputSchema": _schema(CONTRACT_PROPS, ["symbol"]),
        "annotations": READ,
    },
    {
        "name": "history",
        "description": "Historical bars. duration like '5 D', '1 M', '1 Y'; barSize like "
        "'1 min', '5 mins', '1 hour', '1 day'; whatToShow TRADES, MIDPOINT, BID, ASK, "
        "ADJUSTED_LAST.",
        "inputSchema": _schema(
            {
                **CONTRACT_PROPS,
                "duration": {"type": "string", "default": "5 D"},
                "barSize": {"type": "string", "default": "1 hour"},
                "whatToShow": {"type": "string", "default": "TRADES"},
                "useRTH": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "default": 300, "description": "Max bars returned (latest)"},
            },
            ["symbol"],
        ),
        "annotations": READ,
    },
    {
        "name": "open_orders",
        "description": "Open orders (all API clients) with status and fill progress.",
        "inputSchema": _schema({}),
        "annotations": READ,
    },
    {
        "name": "executions",
        "description": "Executions (fills) with commissions, optionally since an ISO time (UTC if "
        "no offset).",
        "inputSchema": _schema({"since": {"type": "string", "description": "e.g. 2026-10-07T13:30:00Z"}}),
        "annotations": READ,
    },
    {
        "name": "preview_order",
        "description": "Step 1 of 2 for a PAPER order. Checks limits and the paper-only gate, runs "
        "IBKR's what-if check, and returns a plain read-back, estimated notional and commission, "
        "and a preview_id valid for 2 minutes. Nothing is sent to the market. Show the read-back "
        "to the user, then call place_order with the preview_id and confirm: true." + HELP_SUFFIX,
        "inputSchema": _schema(ORDER_PROPS, ["symbol", "action", "quantity", "orderType"]),
        "annotations": READ,
    },
    {
        "name": "place_order",
        "description": "Step 2 of 2: submit a previewed order to the IBKR PAPER account. Needs the "
        "preview_id from preview_order (valid 2 minutes, single use) and confirm: true. Re-checks "
        "that every Gateway account is a paper account before sending." + HELP_SUFFIX,
        "inputSchema": _schema(
            {"preview_id": {"type": "string"}, "confirm": {"type": "boolean", "const": True}},
            ["preview_id", "confirm"],
        ),
        "annotations": WRITE,
    },
    {
        "name": "modify_order",
        "description": "Change an open PAPER order placed by this server. Call it first with the "
        "orderId and the changes (quantity, limitPrice, stopPrice, tif, outsideRth): that returns "
        "a read-back and a preview_id and sends nothing. Then call it again with orderId, "
        "preview_id and confirm: true to send the change." + HELP_SUFFIX,
        "inputSchema": _schema(
            {
                "orderId": {"type": "integer"},
                "quantity": {"type": "number"},
                "limitPrice": {"type": "number"},
                "stopPrice": {"type": "number"},
                "tif": {"type": "string", "enum": ["DAY", "GTC"]},
                "outsideRth": {"type": "boolean"},
                "preview_id": {"type": "string"},
                "confirm": {"type": "boolean"},
            },
            ["orderId"],
        ),
        "annotations": WRITE,
    },
    {
        "name": "cancel_order",
        "description": "Cancel an open PAPER order placed by this server. Needs confirm: true."
        + HELP_SUFFIX,
        "inputSchema": _schema(
            {"orderId": {"type": "integer"}, "confirm": {"type": "boolean", "const": True}},
            ["orderId", "confirm"],
        ),
        "annotations": WRITE,
    },
    {
        "name": "paper_log",
        "description": "Latest entries of the local paper-order audit log (previews, orders, "
        "modifications, cancels, refusals, fills).",
        "inputSchema": _schema({"limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 500}}),
        "annotations": READ,
    },
]

TOOL_NAMES = {t["name"] for t in TOOLS}


class App:
    """Tool handlers. `elicit` is set by the RPC layer: async (message, schema) -> dict | None."""

    def __init__(self, cfg, broker: Broker | None = None, audit: AuditLog | None = None,
                 clock=time.monotonic, now=None):
        self.cfg = cfg
        self.broker = broker or Broker(cfg)
        self.audit = audit or AuditLog(cfg.data_dir)
        self.clock = clock
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.rate = policy.RateLimiter(cfg.max_orders_per_min, clock)
        self.previews: dict = {}
        self.elicit = None  # async callable or None
        self.elicitation_supported = lambda: False

    # -- dispatch -------------------------------------------------------------
    async def call(self, name: str, args: dict) -> dict:
        handler = getattr(self, "t_" + name, None)
        if name not in TOOL_NAMES or handler is None:
            raise KeyError(name)
        if not isinstance(args, dict):
            args = {}
        try:
            return await handler(args)
        except PolicyError as e:
            return result(str(e), {"refused": True, "reason": str(e)}, is_error=True)
        except GatewayUnavailable as e:
            return result(str(e), {"connected": False, "error": str(e)}, is_error=True)
        except ToolError as e:
            return result(f"Error: {e}", {"error": str(e)}, is_error=True)

    # -- reads ------------------------------------------------------------
    async def t_gateway_status(self, args):
        cfg = self.cfg
        data = {
            "host": cfg.host,
            "port": cfg.port,
            "port_kind": policy.port_kind(cfg.port),
            "clientId": cfg.client_id,
            "connected": False,
            "limits": cfg.limits(),
            "elicitation": bool(self.elicitation_supported()) and not cfg.auto,
            "audit_log": str(self.audit.path),
        }
        try:
            self.broker.endpoint()
        except PolicyError as e:
            data.update(endpoint_allowed=False, order_submission_allowed=False, why=str(e))
            return result(f"Not connecting: {e}", data)
        data["endpoint_allowed"] = True
        try:
            ib = await self.broker.ensure()
            accounts = await self.broker.fresh_accounts()
        except (GatewayUnavailable, PolicyError) as e:
            data.update(order_submission_allowed=False, why=str(e))
            return result(f"Not connected. {e}", data)
        data["connected"] = True
        try:
            data["server_time"] = str(await self.broker.call(ib.reqCurrentTimeAsync(), "time"))
        except Exception as e:
            data["server_time"] = None
            data["server_time_error"] = str(e)
        data["accounts"] = [
            {"id": a, "type": "paper" if policy.is_paper_account(a) else "LIVE"} for a in accounts
        ]
        try:
            policy.paper_gate(accounts, accounts[0] if accounts else None)
            data["order_submission_allowed"] = True
            data["why"] = "Every account in this Gateway session is a paper account (DU…/DF…)."
        except PolicyError as e:
            data["order_submission_allowed"] = False
            data["why"] = str(e)
        if self.broker.recent_errors:
            data["recent_gateway_messages"] = self.broker.recent_errors[-5:]
        verdict = "paper orders allowed" if data["order_submission_allowed"] else "orders REFUSED"
        return result(
            f"Connected to {data['port_kind']} at {cfg.host}:{cfg.port}; accounts "
            f"{', '.join(a['id'] + ' (' + a['type'] + ')' for a in data['accounts']) or 'none'}; "
            f"{verdict}.",
            data,
        )

    async def t_account_summary(self, args):
        ib = await self.broker.ensure()
        account = _str_arg(args, "account") or ""
        values = await self.broker.call(ib.accountSummaryAsync(account), "account summary")
        out: dict = {}
        for v in values:
            if account and v.account != account:
                continue
            if v.tag not in SUMMARY_TAGS:
                continue
            val = num(v.value)
            entry = val if val is not None else v.value
            out.setdefault(v.account, {})[v.tag] = (
                {"value": entry, "currency": v.currency} if v.currency else entry
            )
        if not out:
            return result("No account summary returned.", {"accounts": {}})
        parts = []
        for acct, tags in out.items():
            nl = tags.get("NetLiquidation")
            nlv = nl["value"] if isinstance(nl, dict) else nl
            parts.append(f"{acct} net liquidation {money(nlv) if isinstance(nlv, float) else nlv}")
        return result("; ".join(parts) + ".", {"accounts": out})

    async def t_positions(self, args):
        ib = await self.broker.ensure()
        account = _str_arg(args, "account")
        pos = await self.broker.call(ib.reqPositionsAsync(), "positions")
        rows = [
            {"account": p.account, "contract": contract_dict(p.contract),
             "position": num(p.position), "avgCost": num(p.avgCost)}
            for p in pos
            if not account or p.account == account
        ]
        return result(f"{len(rows)} position(s).", {"positions": rows})

    async def t_portfolio(self, args):
        ib = await self.broker.ensure()
        account = _str_arg(args, "account") or ""
        items = ib.portfolio(account) if account else ib.portfolio()
        rows = [
            {
                "account": i.account,
                "contract": contract_dict(i.contract),
                "position": num(i.position),
                "marketPrice": num(i.marketPrice),
                "marketValue": num(i.marketValue),
                "averageCost": num(i.averageCost),
                "unrealizedPNL": num(i.unrealizedPNL),
                "realizedPNL": num(i.realizedPNL),
            }
            for i in items
        ]
        total_mv = sum(r["marketValue"] or 0 for r in rows)
        total_u = sum(r["unrealizedPNL"] or 0 for r in rows)
        return result(
            f"{len(rows)} holding(s); market value {money(total_mv)}, unrealized P&L {money(total_u)}.",
            {"portfolio": rows, "totals": {"marketValue": total_mv, "unrealizedPNL": total_u}},
        )

    async def _qualify(self, ib, contract):
        try:
            res = await self.broker.call(ib.qualifyContractsAsync(contract), "contract lookup")
        except GatewayUnavailable:
            raise
        except Exception as e:
            raise ToolError(f"Contract lookup failed: {e}")
        found = [c for c in (res or []) if isinstance(c, Contract) and c.conId]
        if not found:
            raise ToolError(
                f"IBKR could not find a unique contract for {contract.symbol} ({contract.secType}). "
                "Check the symbol, or pass primaryExchange."
            )
        return found[0]

    async def _snapshot(self, ib, contract):
        try:
            tickers = await self.broker.call(ib.reqTickersAsync(contract), "quote")
        except GatewayUnavailable:
            return None
        return tickers[0] if tickers else None

    async def t_quote(self, args):
        contract, _ = build_contract(args)
        ib = await self.broker.ensure()
        contract = await self._qualify(ib, contract)
        t = await self._snapshot(ib, contract)
        if t is None:
            raise ToolError(f"No quote returned for {contract.symbol}.")
        mdt = MD_TYPES.get(getattr(t, "marketDataType", 0), "unknown")
        data = {
            "contract": contract_dict(contract),
            "bid": num(t.bid), "ask": num(t.ask), "last": num(t.last), "close": num(t.close),
            "high": num(t.high), "low": num(t.low), "volume": num(t.volume),
            "time": str(t.time) if getattr(t, "time", None) else None,
            "marketDataType": mdt,
        }
        if mdt.startswith("delayed"):
            data["note"] = "Delayed data (no live market-data subscription for this instrument)."
        px = data["last"] or ticker_price(t)
        return result(
            f"{contract.symbol}: last {px if px is not None else '?'} bid {data['bid']} "
            f"ask {data['ask']} ({mdt} data).",
            data,
        )

    async def t_history(self, args):
        contract, _ = build_contract(args)
        duration = _str_arg(args, "duration") or "5 D"
        bar = _str_arg(args, "barSize") or "1 hour"
        what = (_str_arg(args, "whatToShow") or "TRADES").upper()
        use_rth = args.get("useRTH", True) is not False
        limit = args.get("limit", 300)
        limit = max(1, min(int(limit) if isinstance(limit, (int, float)) else 300, 2000))
        ib = await self.broker.ensure()
        contract = await self._qualify(ib, contract)
        try:
            bars = await asyncio.wait_for(
                ib.reqHistoricalDataAsync(contract, "", duration, bar, what, use_rth),
                timeout=max(self.cfg.request_timeout, 60),
            )
        except (asyncio.TimeoutError, TimeoutError):
            raise GatewayUnavailable("IB Gateway did not return historical data in time.")
        rows = [
            {"t": str(b.date), "o": num(b.open), "h": num(b.high), "l": num(b.low),
             "c": num(b.close), "v": num(b.volume)}
            for b in (bars or [])
        ]
        total = len(rows)
        rows = rows[-limit:]
        return result(
            f"{contract.symbol}: {total} {bar} bar(s) over {duration} ({what})"
            + (f", returning latest {len(rows)}" if total > len(rows) else "") + ".",
            {"contract": contract_dict(contract), "bars": rows},
        )

    async def t_open_orders(self, args):
        ib = await self.broker.ensure()
        trades = await self.broker.call(ib.reqAllOpenOrdersAsync(), "open orders")
        rows = [trade_dict(t) for t in trades]
        return result(f"{len(rows)} open order(s).", {"orders": rows})

    async def t_executions(self, args):
        since_raw = _str_arg(args, "since")
        since = None
        flt = ExecutionFilter()
        if since_raw:
            try:
                since = datetime.fromisoformat(since_raw.replace("Z", "+00:00"))
            except ValueError:
                raise ToolError("`since` must be an ISO date/time, e.g. 2026-10-07T13:30:00Z.")
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            flt.time = since.astimezone(timezone.utc).strftime("%Y%m%d-%H:%M:%S")
        ib = await self.broker.ensure()
        fills = await self.broker.call(ib.reqExecutionsAsync(flt), "executions")
        rows = []
        for f in fills:
            ft = f.time
            if since and isinstance(ft, datetime):
                ft = ft if ft.tzinfo else ft.replace(tzinfo=timezone.utc)
                if ft < since:
                    continue
            rows.append(fill_dict(f))
        return result(f"{len(rows)} execution(s).", {"executions": rows})

    async def t_paper_log(self, args):
        limit = args.get("limit", 20)
        limit = max(1, min(int(limit) if isinstance(limit, (int, float)) else 20, 500))
        entries = self.audit.tail(limit)
        return result(f"{len(entries)} audit entr{'y' if len(entries) == 1 else 'ies'} "
                      f"from {self.audit.path}.", {"entries": entries})

    # -- order helpers ----------------------------------------------------
    async def _gate(self, target):
        accounts = await self.broker.fresh_accounts()
        return policy.paper_gate(accounts, target), accounts

    async def _in_rth(self, ib, contract) -> bool:
        sessions = None
        try:
            details = await asyncio.wait_for(ib.reqContractDetailsAsync(contract), timeout=8)
            if details:
                sessions = details[0].liquidSessions()
        except Exception:
            sessions = None
        return policy.is_regular_hours(self.now(), sessions)

    async def _price_checks(self, ib, contract, p) -> dict:
        """Notional + hours checks. Returns pricing info."""
        mult = 1.0
        if contract.secType == "OPT":
            mult = num(contract.multiplier) or 100.0
        ticker = await self._snapshot(ib, contract)
        ref = ticker_price(ticker)
        order_px = p["limitPrice"] if p["limitPrice"] is not None else p["stopPrice"]
        candidates = [x for x in (order_px, ref) if x]
        if p["orderType"] == "STP_LMT" and p["stopPrice"]:
            candidates.append(p["stopPrice"])
        if not candidates:
            raise PolicyError(
                f"Refused: no price available for {contract.symbol} to estimate the notional of a "
                "market order. Use a limit order."
            )
        notional = p["quantity"] * mult * max(candidates)
        policy.check_notional(self.cfg, notional)
        if p["orderType"] == "MKT":
            in_rth = await self._in_rth(ib, contract)
            policy.check_market_hours(self.cfg, "MKT", p["outsideRth"], in_rth)
        return {
            "referencePrice": ref,
            "marketDataType": MD_TYPES.get(getattr(ticker, "marketDataType", 0)) if ticker else None,
            "multiplier": mult,
            "estimatedNotional": round(notional, 2),
        }

    async def _what_if(self, ib, contract, order) -> dict:
        try:
            st = await asyncio.wait_for(ib.whatIfOrderAsync(contract, order), timeout=self.cfg.request_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return {"whatIf": "unavailable (Gateway did not answer in time)"}
        except Exception as e:
            raise PolicyError(f"Refused: IBKR's what-if check rejected the order: {e}")
        if st is None or isinstance(st, list) and not st:
            return {"whatIf": "unavailable"}
        out = {
            "estimatedCommission": num(st.commission) or num(st.maxCommission) or num(st.minCommission),
            "commissionCurrency": st.commissionCurrency or None,
            "initMarginChange": num(st.initMarginChange),
            "maintMarginChange": num(st.maintMarginChange),
            "equityWithLoanAfter": num(st.equityWithLoanAfter),
        }
        if st.warningText:
            out["warning"] = st.warningText
        if (st.status or "").lower() in ("rejected", "cancelled"):
            raise PolicyError(f"Refused: IBKR's what-if check returned status {st.status}. {st.warningText or ''}".strip())
        return {k: v for k, v in out.items() if v is not None}

    def _new_preview(self, kind, contract, params, account, info, order_id=None) -> str:
        pid = "pv_" + secrets.token_hex(8)
        now = self.clock()
        for k in [k for k, v in self.previews.items() if now - v["created"] > self.cfg.preview_ttl]:
            del self.previews[k]
        self.previews[pid] = {
            "kind": kind, "created": now, "contract": contract, "params": params,
            "account": account, "info": info, "orderId": order_id,
        }
        return pid

    def _take_preview(self, pid, kind) -> dict:
        if not isinstance(pid, str) or not pid:
            raise PolicyError("Refused: `preview_id` is required. Call preview_order first.")
        pv = self.previews.get(pid)
        if pv is None:
            raise PolicyError(
                f"Refused: unknown or already used preview_id {pid!r}. Call preview_order again."
            )
        if self.clock() - pv["created"] > self.cfg.preview_ttl:
            del self.previews[pid]
            raise PolicyError(
                f"Refused: preview {pid} expired (valid {self.cfg.preview_ttl:.0f} s). Preview again."
            )
        if pv["kind"] != kind:
            raise PolicyError(f"Refused: preview {pid} is for {pv['kind']}, not {kind}.")
        return pv

    def _require_confirm(self, args):
        if args.get("confirm") is not True:
            raise PolicyError("Refused: `confirm` must be the boolean true. Nothing was sent.")

    async def _ask_user(self, title: str, text: str) -> None:
        """Approve/Cancel prompt through elicitation when available (skipped in AUTO mode)."""
        if self.cfg.auto or self.elicit is None or not self.elicitation_supported():
            return
        schema = {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "title": "Decision", "enum": ["Approve", "Cancel"]}
            },
            "required": ["decision"],
        }
        try:
            reply = await self.elicit(f"{title}\n\n{text}\n\nApprove to send it to your IBKR PAPER account.", schema)
        except Exception as e:
            raise PolicyError(f"Cancelled: the approval prompt failed ({e}). Nothing was sent.")
        ok = (
            isinstance(reply, dict)
            and reply.get("action") == "accept"
            and isinstance(reply.get("content"), dict)
            and reply["content"].get("decision") == "Approve"
        )
        if not ok:
            raise PolicyError("Cancelled by the user at the approval prompt. Nothing was sent.")

    def _log(self, **entry):
        return self.audit.append(entry)

    def _attach_fill_logger(self, trade):
        def on_fill(tr, fill):
            self._log(event="fill", orderId=tr.order.orderId, account=tr.order.account,
                      fill=fill_dict(fill))

        try:
            trade.fillEvent += on_fill
        except Exception:
            pass

    async def _wait_status(self, trade, seconds=3.0):
        end = self.clock() + seconds
        while trade.orderStatus.status in ("", "PendingSubmit", "ApiPending") and self.clock() < end:
            await asyncio.sleep(0.1)
        return trade.orderStatus.status

    async def _open_trade(self, ib, order_id):
        if not isinstance(order_id, int) or isinstance(order_id, bool) or order_id <= 0:
            raise ToolError("`orderId` must be a positive integer.")
        try:
            await self.broker.call(ib.reqOpenOrdersAsync(), "open orders")
        except GatewayUnavailable:
            pass
        for t in ib.openTrades():
            if t.order.orderId == order_id:
                return t
        raise PolicyError(
            f"Refused: order {order_id} is not an open order placed by this connection "
            f"(clientId {self.cfg.client_id}). Only this server's own open orders can be changed."
        )

    # -- preview / place ----------------------------------------------------
    async def t_preview_order(self, args):
        entry = {"event": "preview", "args": {k: v for k, v in args.items()}}
        try:
            contract, sec = build_contract(args)
            p = parse_order_args(args)
            policy.check_sec_type(self.cfg, sec)
            policy.check_quantity(self.cfg, contract.secType, p["quantity"])
            policy.check_market_hours(self.cfg, p["orderType"], p["outsideRth"], True)
            ib = await self.broker.ensure()
            account, accounts = await self._gate(_str_arg(args, "account"))
            entry.update(account=account, accounts_seen=accounts)
            contract = await self._qualify(ib, contract)
            policy.check_us(self.cfg, contract)
            info = await self._price_checks(ib, contract, p)
            order = make_order(p, account)
            info.update(await self._what_if(ib, contract, order))
        except (PolicyError, ToolError, GatewayUnavailable) as e:
            self._log(**entry, decision="refused", reason=str(e))
            raise
        text = readback(p, contract, account)
        pid = self._new_preview("place", contract, p, account, info)
        data = {
            "preview_id": pid,
            "expires_in_seconds": self.cfg.preview_ttl,
            "readback": text,
            "account": account,
            "contract": contract_dict(contract),
            "order": p,
            **info,
            "next": "Show this read-back to the user. To send it: place_order "
            f"{{\"preview_id\": \"{pid}\", \"confirm\": true}}.",
        }
        self._log(**entry, decision="allowed", previewId=pid, readback=text, order=p,
                  contract=contract_dict(contract), estimate=info)
        comm = info.get("estimatedCommission")
        return result(
            f"PREVIEW (not sent): {text}. Est. notional {money(info['estimatedNotional'])}"
            + (f", est. commission {money(comm)}" if comm is not None else "")
            + f". preview_id {pid} (valid {self.cfg.preview_ttl:.0f} s).",
            data,
        )

    async def t_place_order(self, args):
        pid = args.get("preview_id")
        entry = {"event": "place", "previewId": pid}
        try:
            self._require_confirm(args)
            pv = self._take_preview(pid, "place")
            p, contract = pv["params"], pv["contract"]
            text = readback(p, contract, pv["account"])
            entry.update(account=pv["account"], order=p, contract=contract_dict(contract), readback=text)
            self.rate.check()
            ib = await self.broker.ensure()
            account, accounts = await self._gate(pv["account"])
            if p["orderType"] == "MKT":
                policy.check_market_hours(self.cfg, "MKT", p["outsideRth"], await self._in_rth(ib, contract))
            await self._ask_user("Place this PAPER order?", text)
            # Re-check right before sending (the prompt may have taken a while).
            account, accounts = await self._gate(pv["account"])
            entry["accounts_seen"] = accounts
            self.rate.check()
            if self.previews.pop(pid, None) is None:
                raise PolicyError(f"Refused: preview {pid} was already used.")
            if not self._log(**entry, decision="submitting"):
                raise PolicyError("Refused: the audit log could not be written, so nothing was sent.")
            order = make_order(p, account)
            trade = ib.placeOrder(contract, order)
            self.rate.record()
        except (PolicyError, ToolError, GatewayUnavailable) as e:
            self._log(**entry, decision="refused", reason=str(e))
            raise
        self._attach_fill_logger(trade)
        status = await self._wait_status(trade)
        oid = trade.order.orderId
        msgs = [le.message for le in getattr(trade, "log", []) if getattr(le, "message", "")]
        self._log(**entry, decision="placed", orderId=oid, status=status, messages=msgs[-3:])
        data = {"orderId": oid, "status": status, "account": account, "readback": text,
                "order": trade_dict(trade)}
        if msgs:
            data["messages"] = msgs[-3:]
        return result(f"PAPER order {oid} sent: {text}. Status: {status or 'unknown'}.", data)

    # -- modify / cancel ------------------------------------------------------
    async def t_modify_order(self, args):
        order_id = args.get("orderId")
        if args.get("preview_id") is None:
            return await self._modify_preview(args, order_id)
        entry = {"event": "modify", "orderId": order_id, "previewId": args.get("preview_id")}
        try:
            self._require_confirm(args)
            pv = self._take_preview(args.get("preview_id"), "modify")
            if pv["orderId"] != order_id:
                raise PolicyError(f"Refused: preview is for order {pv['orderId']}, not {order_id}.")
            p, contract = pv["params"], pv["contract"]
            text = readback(p, contract, pv["account"])
            entry.update(account=pv["account"], order=p, readback=text)
            self.rate.check()
            ib = await self.broker.ensure()
            await self._gate(pv["account"])
            await self._ask_user(f"Modify PAPER order {order_id}?", text)
            account, accounts = await self._gate(pv["account"])
            entry["accounts_seen"] = accounts
            trade = await self._open_trade(ib, order_id)
            if (trade.order.account or "") != account:
                raise PolicyError(f"Refused: order {order_id} belongs to account {trade.order.account!r}.")
            if self.previews.pop(args.get("preview_id"), None) is None:
                raise PolicyError("Refused: preview was already used.")
            if not self._log(**entry, decision="submitting"):
                raise PolicyError("Refused: the audit log could not be written, so nothing was sent.")
            new = copy.copy(trade.order)
            new.totalQuantity = p["quantity"]
            new.tif = p["tif"]
            new.outsideRth = p["outsideRth"]
            if p["limitPrice"] is not None:
                new.lmtPrice = p["limitPrice"]
            if p["stopPrice"] is not None:
                new.auxPrice = p["stopPrice"]
            new.transmit = True
            trade = ib.placeOrder(trade.contract, new)
            self.rate.record()
        except (PolicyError, ToolError, GatewayUnavailable) as e:
            self._log(**entry, decision="refused", reason=str(e))
            raise
        status = await self._wait_status(trade)
        self._log(**entry, decision="modified", status=status)
        return result(f"PAPER order {order_id} modified: {text}. Status: {status or 'unknown'}.",
                      {"orderId": order_id, "status": status, "order": trade_dict(trade)})

    async def _modify_preview(self, args, order_id):
        entry = {"event": "modify_preview", "orderId": order_id,
                 "changes": {k: v for k, v in args.items() if k not in ("orderId", "confirm")}}
        try:
            changes = {k: args[k] for k in ("quantity", "limitPrice", "stopPrice", "tif", "outsideRth") if k in args}
            if not changes:
                raise ToolError("Give at least one change: quantity, limitPrice, stopPrice, tif or outsideRth.")
            ib = await self.broker.ensure()
            if not isinstance(order_id, int) or isinstance(order_id, bool) or order_id <= 0:
                raise ToolError("`orderId` must be a positive integer.")
            accounts = await self.broker.fresh_accounts()
            trade = await self._open_trade(ib, order_id)
            account = policy.paper_gate(accounts, trade.order.account or "")
            entry.update(account=account, accounts_seen=accounts)
            current = order_params_from(trade.order)
            merged = {**current, **{k: v for k, v in changes.items()}}
            merged["action"] = current["action"]
            p = parse_order_args(merged)
            contract = trade.contract
            policy.check_sec_type(self.cfg, contract.secType)
            policy.check_quantity(self.cfg, contract.secType, p["quantity"])
            policy.check_us(self.cfg, contract)
            info = await self._price_checks(ib, contract, p)
            whatif_order = make_order(p, account)
            info.update(await self._what_if(ib, contract, whatif_order))
        except (PolicyError, ToolError, GatewayUnavailable) as e:
            self._log(**entry, decision="refused", reason=str(e))
            raise
        text = readback(p, contract, account)
        pid = self._new_preview("modify", contract, p, account, info, order_id=order_id)
        self._log(**entry, decision="allowed", previewId=pid, readback=text, before=current, after=p)
        return result(
            f"MODIFY PREVIEW (not sent) for order {order_id}: {text}. Est. notional "
            f"{money(info['estimatedNotional'])}. preview_id {pid} (valid {self.cfg.preview_ttl:.0f} s).",
            {"preview_id": pid, "orderId": order_id, "before": current, "after": p,
             "readback": text, **info,
             "next": f"To send it: modify_order {{\"orderId\": {order_id}, \"preview_id\": \"{pid}\", \"confirm\": true}}."},
        )

    async def t_cancel_order(self, args):
        order_id = args.get("orderId")
        entry = {"event": "cancel", "orderId": order_id}
        try:
            self._require_confirm(args)
            ib = await self.broker.ensure()
            trade = await self._open_trade(ib, order_id)
            accounts = await self.broker.fresh_accounts()
            account = policy.paper_gate(accounts, trade.order.account or "")
            entry.update(account=account, accounts_seen=accounts, order=order_params_from(trade.order),
                         contract=contract_dict(trade.contract))
            if not self._log(**entry, decision="submitting"):
                raise PolicyError("Refused: the audit log could not be written, so nothing was sent.")
            ib.cancelOrder(trade.order)
        except (PolicyError, ToolError, GatewayUnavailable) as e:
            self._log(**entry, decision="refused", reason=str(e))
            raise
        status = await self._wait_status(trade, 2.0)
        self._log(**entry, decision="cancel_sent", status=status)
        return result(f"Cancel sent for PAPER order {order_id}. Status: {status or 'unknown'}.",
                      {"orderId": order_id, "status": status})
