"""Configuration from environment variables.

Every limit has a conservative default. An invalid or non-positive value falls
back to the default (and logs a warning to stderr) instead of loosening a limit.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

SERVER_NAME = "ibkr-paper-gateway"
SERVER_VERSION = "0.1.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4002
DEFAULT_CLIENT_ID = 17


def _warn(msg: str) -> None:
    print(f"[{SERVER_NAME}] warning: {msg}", file=sys.stderr, flush=True)


def _flag(env, name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _positive(env, name: str, default: float, *, integer: bool = False):
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        _warn(f"{name}={raw!r} is not a number; using default {default}")
        return default
    if value <= 0:
        _warn(f"{name}={raw!r} must be positive; using default {default}")
        return default
    return value


def default_data_dir(env=None) -> Path:
    env = os.environ if env is None else env
    for key in ("CLAUDE_PLUGIN_DATA", "PLUGIN_DATA"):
        value = env.get(key)
        if value:
            return Path(value).expanduser()
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / SERVER_NAME


@dataclass
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    port_from_env: bool = False
    client_id: int = DEFAULT_CLIENT_ID
    max_qty_stk: float = 100
    max_qty_opt: float = 10
    max_notional: float = 50_000.0
    max_orders_per_min: int = 10
    sec_types: frozenset = field(default_factory=lambda: frozenset({"STK", "ETF"}))
    us_only: bool = True
    allow_mkt_outside_rth: bool = False
    auto: bool = False
    data_dir: Path = field(default_factory=default_data_dir)
    preview_ttl: float = 120.0
    connect_timeout: float = 8.0
    request_timeout: float = 15.0
    elicit_timeout: float = 300.0

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env
        cfg = cls(data_dir=default_data_dir(env))
        host = env.get("IBKR_PAPER_HOST", "").strip()
        if host:
            cfg.host = host
        raw_port = env.get("IBKR_PAPER_PORT", "").strip()
        if raw_port:
            try:
                cfg.port = int(raw_port)
                cfg.port_from_env = True
            except ValueError:
                # An unparseable port must not silently become 4002 on a
                # different machine setup; keep it invalid so the port policy refuses.
                _warn(f"IBKR_PAPER_PORT={raw_port!r} is not a port number")
                cfg.port = -1
                cfg.port_from_env = True
        raw_cid = env.get("IBKR_CLIENT_ID", "").strip()
        if raw_cid:
            try:
                cfg.client_id = int(raw_cid)
            except ValueError:
                _warn(f"IBKR_CLIENT_ID={raw_cid!r} is not an integer; using {DEFAULT_CLIENT_ID}")
        cfg.max_qty_stk = _positive(env, "IBKR_PAPER_MAX_QTY", cfg.max_qty_stk)
        cfg.max_qty_opt = _positive(env, "IBKR_PAPER_MAX_OPT_QTY", cfg.max_qty_opt)
        cfg.max_notional = _positive(env, "IBKR_PAPER_MAX_NOTIONAL", cfg.max_notional)
        cfg.max_orders_per_min = _positive(
            env, "IBKR_PAPER_MAX_ORDERS_PER_MIN", cfg.max_orders_per_min, integer=True
        )
        raw_types = env.get("IBKR_PAPER_SEC_TYPES", "").strip()
        types = (
            {t.strip().upper() for t in raw_types.split(",") if t.strip()}
            if raw_types
            else {"STK", "ETF"}
        )
        known = {"STK", "ETF", "OPT"}
        unknown = types - known
        if unknown:
            _warn(f"IBKR_PAPER_SEC_TYPES: ignoring unsupported type(s) {sorted(unknown)}")
        types &= known
        if "OPT" in types and not _flag(env, "IBKR_PAPER_ALLOW_OPT"):
            _warn("OPT in IBKR_PAPER_SEC_TYPES also needs IBKR_PAPER_ALLOW_OPT=1; options stay off")
            types.discard("OPT")
        if _flag(env, "IBKR_PAPER_ALLOW_OPT"):
            types.add("OPT")
        cfg.sec_types = frozenset(types or {"STK", "ETF"})
        cfg.us_only = _flag(env, "IBKR_PAPER_US_ONLY", True)
        cfg.allow_mkt_outside_rth = _flag(env, "IBKR_PAPER_ALLOW_MKT_OUTSIDE_RTH")
        cfg.auto = _flag(env, "IBKR_PAPER_AUTO")
        return cfg

    def limits(self) -> dict:
        return {
            "max_qty_shares": self.max_qty_stk,
            "max_qty_option_contracts": self.max_qty_opt,
            "max_notional_usd": self.max_notional,
            "max_orders_per_minute": self.max_orders_per_min,
            "allowed_sec_types": sorted(self.sec_types),
            "us_only": self.us_only,
            "market_orders_outside_rth": self.allow_mkt_outside_rth,
            "auto_mode": self.auto,
            "preview_valid_seconds": self.preview_ttl,
        }
