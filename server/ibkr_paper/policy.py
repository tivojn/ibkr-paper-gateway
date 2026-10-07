"""Safety policy: endpoint (host/port), paper-account gate, order limits, rate limit, hours.

Pure functions and small classes with no I/O, so they are easy to test.
"""

from __future__ import annotations

import ipaddress
import time
from collections import deque
from datetime import datetime, time as dtime

LIVE_PORTS = {4001: "IB Gateway LIVE", 7496: "TWS LIVE"}
PAPER_PORTS = {4002: "IB Gateway paper", 7497: "TWS paper"}
PAPER_PREFIXES = ("DU", "DF")

GATEWAY_HELP = (
    "Start IB Gateway, log in with your PAPER login, API port 4002, Read-Only API off "
    "(Configure > Settings > API > Settings; allow connections from localhost only)."
)

# Primary listing exchanges treated as US for the US-only rule (OTC/pink sheets excluded).
US_EXCHANGES = {
    "NYSE", "NASDAQ", "ISLAND", "ARCA", "NYSEARCA", "AMEX", "NYSEAMERICAN", "BATS", "BYX",
    "EDGEA", "EDGX", "IEX", "NYSENAT", "CBOE", "MEMX", "LTSE", "PSX", "CHX", "BEX", "IBKRATS",
}


class PolicyError(Exception):
    """A refusal. The message is shown to the user as-is."""


def is_loopback(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:
        return False


def connect_host(host: str) -> str:
    """The address actually dialled. 'localhost' is pinned to IPv4 loopback."""
    h = host.strip().strip("[]")
    return "127.0.0.1" if h.lower() == "localhost" else h


def port_kind(port: int) -> str:
    if port in LIVE_PORTS:
        return LIVE_PORTS[port]
    if port in PAPER_PORTS:
        return PAPER_PORTS[port]
    return "custom (allowed via IBKR_PAPER_PORT; paper-account gate still applies)"


def check_endpoint(host: str, port: int) -> str:
    """Raise PolicyError unless host is loopback and port is not a live port."""
    if not is_loopback(host):
        raise PolicyError(
            f"Refused: host {host!r} is not loopback. This server only connects to IB Gateway/TWS "
            "on this computer (127.0.0.1)."
        )
    if port in LIVE_PORTS:
        raise PolicyError(
            f"Refused: port {port} is the {LIVE_PORTS[port]} API port. This server never connects "
            "to live ports. Use 4002 (IB Gateway paper) or 7497 (TWS paper)."
        )
    if not (0 < port < 65536):
        raise PolicyError(f"Refused: {port} is not a valid TCP port.")
    return port_kind(port)


def is_paper_account(account_id: str) -> bool:
    return isinstance(account_id, str) and account_id.strip().startswith(PAPER_PREFIXES)


def paper_gate(accounts, target: str | None = None) -> str:
    """Return the paper account to use, or raise PolicyError.

    accounts must be a fresh managedAccounts list. Every account must be a paper
    account (DU/DF), and the target (if given) must be one of them.
    """
    accounts = [a.strip() for a in (accounts or []) if isinstance(a, str) and a.strip()]
    if not accounts:
        raise PolicyError(
            "Refused: IB Gateway reported no managed accounts, so this cannot be confirmed as a "
            "paper-only session."
        )
    live = [a for a in accounts if not is_paper_account(a)]
    if live:
        raise PolicyError(
            "Refused: this Gateway session can see non-paper (LIVE) account(s) "
            f"{', '.join(live)}. Orders are allowed only when every account is a paper account "
            "(DU…/DF…). Log out and log in to IB Gateway with your PAPER login."
        )
    if target is None or str(target).strip() == "":
        if len(accounts) == 1:
            return accounts[0]
        raise PolicyError(
            f"Refused: several paper accounts are available ({', '.join(accounts)}); pass `account`."
        )
    target = str(target).strip()
    if target not in accounts:
        raise PolicyError(
            f"Refused: account {target!r} is not one of this session's paper accounts "
            f"({', '.join(accounts)})."
        )
    return target


def check_quantity(cfg, sec_type: str, quantity: float) -> None:
    limit = cfg.max_qty_opt if sec_type == "OPT" else cfg.max_qty_stk
    unit = "contracts" if sec_type == "OPT" else "shares"
    if quantity > limit:
        raise PolicyError(
            f"Refused: quantity {quantity:g} exceeds the per-order limit of {limit:g} {unit} "
            "(IBKR_PAPER_MAX_QTY / IBKR_PAPER_MAX_OPT_QTY)."
        )


def check_notional(cfg, notional: float) -> None:
    if notional > cfg.max_notional:
        raise PolicyError(
            f"Refused: estimated notional ${notional:,.2f} exceeds the per-order limit of "
            f"${cfg.max_notional:,.2f} (IBKR_PAPER_MAX_NOTIONAL)."
        )


def check_sec_type(cfg, sec_type: str) -> None:
    # ETFs are STK contracts at IBKR, so STK and ETF are treated as the same type.
    allowed = {"STK" if t == "ETF" else t for t in cfg.sec_types}
    if ("STK" if sec_type == "ETF" else sec_type) not in allowed:
        hint = " Options need IBKR_PAPER_ALLOW_OPT=1." if sec_type == "OPT" else ""
        raise PolicyError(
            f"Refused: security type {sec_type} is not allowed for orders "
            f"(allowed: {', '.join(sorted(cfg.sec_types))}).{hint}"
        )


def check_us(cfg, contract) -> None:
    if not cfg.us_only:
        return
    currency = (getattr(contract, "currency", "") or "").upper()
    if currency != "USD":
        raise PolicyError(
            f"Refused: only US (USD) instruments are allowed (got currency {currency or '?'}). "
            "Set IBKR_PAPER_US_ONLY=0 to change this."
        )
    primary = (getattr(contract, "primaryExchange", "") or "").upper()
    if getattr(contract, "secType", "") == "STK" and primary and primary not in US_EXCHANGES:
        raise PolicyError(
            f"Refused: {getattr(contract, 'symbol', '?')} lists on {primary}, which is not a US "
            "exchange covered by the US-only rule."
        )


class RateLimiter:
    """Sliding 60-second window of submitted orders."""

    def __init__(self, max_per_min: int, clock=time.monotonic):
        self.max = max_per_min
        self.clock = clock
        self.events: deque = deque()

    def _trim(self) -> None:
        now = self.clock()
        while self.events and now - self.events[0] >= 60:
            self.events.popleft()

    def check(self) -> None:
        self._trim()
        if len(self.events) >= self.max:
            raise PolicyError(
                f"Refused: rate limit of {self.max} orders per minute reached "
                "(IBKR_PAPER_MAX_ORDERS_PER_MIN). Try again shortly."
            )

    def record(self) -> None:
        self.events.append(self.clock())


def _eastern():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:  # pragma: no cover - tzdata missing
        return None


def is_regular_hours(now: datetime, sessions=None) -> bool:
    """True if now (tz-aware) falls in a regular (liquid) session.

    sessions: list of objects with tz-aware .start/.end (ib_async TradingSession),
    from contract details. Without them, falls back to US equities 09:30-16:00 ET,
    Monday-Friday (no holiday calendar). Unknown time zone -> False (conservative).
    """
    if sessions:
        return any(s.start <= now < s.end for s in sessions)
    tz = _eastern()
    if tz is None:
        return False
    et = now.astimezone(tz)
    return et.weekday() < 5 and dtime(9, 30) <= et.time() < dtime(16, 0)


def check_market_hours(cfg, order_type: str, outside_rth: bool, in_rth: bool) -> None:
    if order_type != "MKT" or cfg.allow_mkt_outside_rth:
        return
    if outside_rth:
        raise PolicyError(
            "Refused: market orders with outsideRth are not allowed. Use a limit order, or set "
            "IBKR_PAPER_ALLOW_MKT_OUTSIDE_RTH=1."
        )
    if not in_rth:
        raise PolicyError(
            "Refused: market orders are allowed only during regular trading hours. Use a limit "
            "order, or set IBKR_PAPER_ALLOW_MKT_OUTSIDE_RTH=1."
        )
