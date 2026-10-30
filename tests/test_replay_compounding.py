"""Attribution must reconcile, and CSV supports open/closed trade fields."""
import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "tools/replay_compounding.py"
spec = importlib.util.spec_from_file_location("replay_compounding", PATH)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def test_tail_attribution_rounds_up_and_reconciles():
    trades = [{"net_pnl": v} for v in range(-5, 6)]
    result = replay.concentration(trades)
    assert result["top_10pct"]["n"] == 2
    assert result["remaining_90pct"]["n"] == 9
    assert result["top_10pct"]["net"] == 9
    assert result["remaining_90pct"]["net"] == -9
    assert result["top_10pct"]["share_of_net"] is None


def test_net_winrate_and_profit_factor():
    result = replay.trade_stats([{"net_pnl": v} for v in (10, -4, 0)])
    assert result["winrate"] == pytest.approx(1 / 3)
    assert result["profit_factor"] == 2.5
    assert result["breakeven"] == 1


def test_csv_accepts_fields_missing_from_first_row(tmp_path):
    path = tmp_path / "trades.csv"
    replay.write_csv(path, [{"net": 1}, {"net": 2, "exit_reason_code": "STOP"}])
    assert "exit_reason_code" in path.read_text(encoding="utf-8-sig")