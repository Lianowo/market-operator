"""Keep live/replay tiers aligned and verify inclusive tier boundaries."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from live_runner import LiveRunner
from market_operator.broker import Broker


@pytest.mark.parametrize("equity, expected", [
    (300, 0.05), (1000, 0.05), (1000.01, 0.04),
    (5000, 0.04), (5000.01, 0.03), (10000, 0.03),
    (10000.01, 0.02), (50000, 0.02), (50000.01, 0.01), (1e9, 0.01),
])
def test_production_compounding_tiers(equity, expected):
    live = json.loads((ROOT / "config/live.json").read_text(encoding="utf-8"))
    strategy = json.loads((ROOT / "config/strategy.json").read_text(encoding="utf-8"))
    risk = live["risk"]
    assert risk["dynamic_risk_tiers"] == strategy["small_account_compounding"]["dynamic_risk_tiers"]
    runner = LiveRunner.__new__(LiveRunner)
    runner.risk, runner.equity = risk, equity
    assert runner.current_risk_frac() == expected
    broker = Broker.__new__(Broker)
    broker.cash, broker.risk_frac = equity, risk["risk_frac"]
    broker.dynamic_risk_tiers = risk["dynamic_risk_tiers"]
    assert broker.current_risk_frac() == expected