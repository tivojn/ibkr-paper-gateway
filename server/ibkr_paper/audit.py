"""Append-only JSON-lines audit log (mode 0600)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_NAME = "paper-orders.log"


class AuditLog:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.path = self.dir / LOG_NAME

    def append(self, entry: dict) -> bool:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **entry}
        line = json.dumps(record, default=str, separators=(",", ":")) + "\n"
        try:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except OSError as e:
            print(f"[ibkr-paper-gateway] audit log write failed: {e}", file=sys.stderr, flush=True)
            return False

    def tail(self, limit: int = 20) -> list:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"unparsed": line.rstrip("\n")})
        return out
