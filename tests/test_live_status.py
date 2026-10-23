"""实盘巡检脚本(`scripts/live_status.py`)判定逻辑测试。

`live_status` 是"保护是否完好"的唯一外部读数:本地 ↔ 交易所 ↔ 计划单三方核对。
它若把错配判成正常,运维就会在裸奔时看到"三者一致";故把每条判定钉住:
止损单不在交易所 / 同品种多张计划单 / 交易所独有仓 / 本地残留仓 /
现价越防线 / 数量不一致,以及心跳年龄的单位(修前除数多写三个 0)。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import live_status as ls                                          # noqa: E402

LOC = {"BTCUSDT": {"direction": -1, "qty": 0.5, "entry": 100.0,
                   "invalid": 110.0, "mark": 99.0}}
EX = {"BTCUSDT": {"direction": -1, "qty": 0.5, "entry_price": 100.0,
                  "mark_price": 99.0}}
STOPS = {"BTCUSDT": {"order_id": "oid-1"}}
PENDING = {"BTCUSDT": [{"orderId": "oid-1"}]}


def _issues(positions=None, stops=None, ex=None, pending=None,
            has_client=True):
    return ls._print_positions(
        LOC if positions is None else positions,
        STOPS if stops is None else stops,
        EX if ex is None else ex,
        PENDING if pending is None else pending,
        has_client)


def test_status_clean_book_has_no_issue():
    assert _issues() == []


def test_status_flags_stop_missing_on_exchange():
    got = _issues(pending={"BTCUSDT": [{"orderId": "someone-else"}]})
    assert any("止损单不在交易所" in s for s in got)


def test_status_flags_duplicate_plan_orders():
    got = _issues(pending={"BTCUSDT": [{"orderId": "oid-1"},
                                       {"orderId": "oid-2"}]})
    assert any("多张计划单" in s for s in got)


def test_status_flags_orphan_and_stale_positions():
    assert any("交易所仓位未被本地接管" in s for s in _issues(positions={}))
    assert any("本地残留仓位" in s for s in _issues(ex={}))


def test_status_flags_no_stop_record():
    assert any("本地无止损记录" in s for s in _issues(stops={}))


def test_status_flags_breached_defense():
    breached = {"BTCUSDT": {"direction": -1, "qty": 0.5, "entry": 100.0,
                            "invalid": 98.0, "mark": 99.0}}
    assert any("现价已越过防线" in s for s in _issues(positions=breached))


def test_status_flags_qty_mismatch():
    ex = {"BTCUSDT": {**EX["BTCUSDT"], "qty": 0.9}}
    assert any("数量与交易所不一致" in s for s in _issues(ex=ex))


def test_age_h_divisor_is_hours():
    """心跳年龄单位必须是小时:修前除数 3_600_000_000,停摆也显示 0.00h。"""
    assert ls._age_h(1, 3_600_001) == "1.00h"
