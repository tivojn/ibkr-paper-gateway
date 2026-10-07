import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ibkr_paper.broker import Broker
from ibkr_paper.config import Config
from ibkr_paper.tools import App

from .fake_ib import FakeIB

# Wednesday 2026-10-07 11:00 New York time (regular hours)
RTH_NOW = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
# Saturday
WEEKEND_NOW = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make_app(fake=None, now=RTH_NOW, **cfg_overrides):
    tmp = tempfile.mkdtemp(prefix="ibkr-paper-test-")
    cfg = Config(data_dir=Path(tmp) / "data")
    for k, v in cfg_overrides.items():
        setattr(cfg, k, v)
    fake = fake or FakeIB()
    clock = Clock()
    app = App(cfg, broker=Broker(cfg, ib_factory=lambda: fake), clock=clock, now=lambda: now)
    app.test_clock = clock
    app.fake = fake
    return app


def payload(res):
    """Split a tool result into (summary line, data dict)."""
    text = res["content"][0]["text"]
    summary, _, js = text.partition("\n")
    return summary, json.loads(js)
