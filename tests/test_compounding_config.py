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
    (500, 0.05), (1000, 0.05), (15000, 0.05),
    (15000.01, 0.042), (100000, 0.042),
    (100000.01, 0.035), (300000, 0.035),
    (300000.01, 0.025), (1e9, 0.025),
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


def test_hwm_tier_never_steps_up_on_drawdown():
    live = json.loads((ROOT / "config/live.json").read_text(encoding="utf-8"))
    risk = live["risk"]
    broker = Broker.__new__(Broker)
    broker.cash, broker.risk_frac = 120000.0, risk["risk_frac"]
    broker.dynamic_risk_tiers = risk["dynamic_risk_tiers"]
    assert broker.current_risk_frac() == 0.035
    # Drawdown below 100000 or 15000 must NOT jump risk rate back up to 0.042 or 0.05
    broker.cash = 5000.0
    assert broker.current_risk_frac() == 0.035