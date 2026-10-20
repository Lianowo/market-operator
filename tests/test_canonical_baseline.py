"""canonical capped-v1 基准重冻结后的对账模式切换测试(research-log #17)。

约束:
- merge_max_atr>0(canonical)时 cross_checks 只以 config/canonical_baseline_counts.json
  为硬对账对象,不再触碰 v1/v2/event_study_v3 历史冻结产物;
- merge_max_atr=0(历史)时 legacy 对账行为一字不变,基准文件即使存在也不被读取;
- 基准文件与 canonical 配置形状一致(15 格 × 8 字段 + totals + 生成元信息)。
"""
import json
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

import run_backtest_v6 as rb                     # noqa: E402
from market_operator.events import MarketEvent  # noqa: E402

K_IS = 0.8


def _e7(direction, invalid, atr=1.0):
    return MarketEvent(id="E7", direction=direction, ts=0, known_index=0,
                       attrs={"invalid": invalid, "atr": atr})


def _e(eid, direction):
    return MarketEvent(id=eid, direction=direction, ts=0, known_index=0,
                       attrs={})


def _frame(symbol, tf, events, close=10.0, atr=1.0):
    cog = SimpleNamespace(events_by={0: events} if events else {},
                          bars=[(0, 9.0, 11.0, 8.0, close, 1.0)], atrs=[atr])
    return SimpleNamespace(symbol=symbol, tf_label=tf, cog=cog)


def _demo_frames():
    # close=10, atr=1:E7(+1, invalid=5) 距 5 ≥ 0.8 过 F1;E7(-1, invalid=5)
    # 距 -5 不过;E7(-1, invalid=12) 距 2 ≥ 0.8 过。
    return {
        "BTCUSDT@1h": _frame("BTCUSDT", "1h", [
            _e7(+1, 5.0), _e7(-1, 5.0), _e7(-1, 12.0),
            _e("E1", -1), _e("E1", -1), _e("E1", +1), _e("E6", -1)]),
        "ETHUSDT@1h": _frame("ETHUSDT", "1h", [_e("E6", +1)]),
        "BTCUSDT@2h": _frame("BTCUSDT", "2h", [_e7(-1, 12.0)]),
    }


CELLS = {
    "BTCUSDT@1h": {"E1_long": 1, "E1_short": 2, "E6_long": 0, "E6_short": 1,
                   "E7_long": 1, "E7_short": 2, "E7_fired": 3, "E7_passed_f1": 2},
    "ETHUSDT@1h": {"E1_long": 0, "E1_short": 0, "E6_long": 1, "E6_short": 0,
                   "E7_long": 0, "E7_short": 0, "E7_fired": 0, "E7_passed_f1": 0},
    "BTCUSDT@2h": {"E1_long": 0, "E1_short": 0, "E6_long": 0, "E6_short": 0,
                   "E7_long": 0, "E7_short": 1, "E7_fired": 1, "E7_passed_f1": 1},
}


def _write_baseline(tmp_path, cells, e1e6_1h=3, merge_max_atr=2.0):
    p = tmp_path / "canonical_baseline_counts.json"
    p.write_text(json.dumps({
        "version": "capped-v1", "generated_at_utc": "2026-10-20T00:00:00+00:00",
        "generator": {"command": "python scripts/freeze_canonical_baseline.py",
                      "merge_max_atr": merge_max_atr, "k_is": K_IS},
        "totals": {"e1e6_short_1h": e1e6_1h}, "cells": cells},
        ensure_ascii=False), encoding="utf-8")
    return p


def _write_legacy_frozen(tmp_path, *, wrong=False):
    """legacy 对账所需三冻结产物(wrong=True 时给出必然不匹配的假值)。"""
    n = 0 if wrong else None
    v2 = tmp_path / "backtest_v2.json"
    v2.write_text(json.dumps({"e7_counts": {
        "BTCUSDT": {"whitelist_events": n if wrong else 3,
                    "passed_f1": n if wrong else 2},
        "ETHUSDT": {"whitelist_events": 0, "passed_f1": 0}}}), encoding="utf-8")
    v1 = tmp_path / "backtest.json"
    v1.write_text(json.dumps({"runs": {"full@1.0x": {"metrics": {"counters": {
        "go_events": n if wrong else 3}}}}}), encoding="utf-8")
    es3 = tmp_path / "event_study_v3.json"
    es3.write_text(json.dumps({"timeframes": {"2h": {"symbols": {
        "BTCUSDT": {"n_events_fired": n if wrong else 1,
                    "n_passed_f1": n if wrong else 1}}}}}), encoding="utf-8")
    return v2, v1, es3


def test_capped_mode_uses_baseline_and_ignores_legacy_frozen(tmp_path,
                                                             monkeypatch):
    monkeypatch.setattr(rb, "BASELINE_PATH", _write_baseline(tmp_path, CELLS))
    v2, v1, es3 = _write_legacy_frozen(tmp_path, wrong=True)   # 全错假值
    monkeypatch.setattr(rb, "BT_V2_PATH", v2)
    monkeypatch.setattr(rb, "BT_V1_PATH", v1)
    monkeypatch.setattr(rb, "ES_V3_PATH", es3)
    out = rb.cross_checks(_demo_frames(), K_IS, merge_max_atr=2.0)
    assert out["baseline"] == "capped-v1"
    assert out["e7_counts_1h"]["BTCUSDT"] == {
        "fired": 3, "passed_f1": 2, "E1_short": 2, "E6_short": 1}
    assert out["e1e6_short_1h"] == {"replay": 3, "baseline": 3}


def test_capped_mode_rejects_baseline_mismatch(tmp_path, monkeypatch):
    bad = json.loads(json.dumps(CELLS))
    bad["BTCUSDT@2h"]["E7_fired"] = 2
    monkeypatch.setattr(rb, "BASELINE_PATH", _write_baseline(tmp_path, bad))
    with pytest.raises(RuntimeError, match="capped-v1 基准对账失败"):
        rb.cross_checks(_demo_frames(), K_IS, merge_max_atr=2.0)


def test_capped_mode_rejects_merge_max_atr_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "BASELINE_PATH",
                        _write_baseline(tmp_path, CELLS, merge_max_atr=1.5))
    with pytest.raises(RuntimeError, match="merge_max_atr"):
        rb.cross_checks(_demo_frames(), K_IS, merge_max_atr=2.0)


def test_legacy_mode_ignores_baseline_file(tmp_path, monkeypatch):
    # 基准文件故意全错:legacy(merge_max_atr=0)不得读取;legacy 冻结产物全对。
    monkeypatch.setattr(rb, "BASELINE_PATH",
                        _write_baseline(tmp_path, json.loads(json.dumps(CELLS)),
                                        e1e6_1h=999))
    v2, v1, es3 = _write_legacy_frozen(tmp_path, wrong=False)
    monkeypatch.setattr(rb, "BT_V2_PATH", v2)
    monkeypatch.setattr(rb, "BT_V1_PATH", v1)
    monkeypatch.setattr(rb, "ES_V3_PATH", es3)
    out = rb.cross_checks(_demo_frames(), K_IS)      # 默认 merge_max_atr=0
    assert "baseline" not in out
    assert out["e1e6_short_vs_v1"] == {"v6": 3, "v1": 3}
    assert out["es_v3_tf_match"] is True


def test_baseline_artifact_matches_canonical_config():
    root = rb.ROOT
    cfg = json.loads((root / "config" / "canonical.json")
                     .read_text(encoding="utf-8"))
    base = json.loads((root / "config" / "canonical_baseline_counts.json")
                      .read_text(encoding="utf-8"))
    assert base["version"] == "capped-v1"
    gen = base["generator"]
    assert gen["command"] == "python scripts/freeze_canonical_baseline.py"
    assert gen["merge_max_atr"] == cfg["merge_max_atr"] == 2.0
    assert gen["k_is"] == cfg["k_is"]
    assert gen["data_manifest"]["sha256"]
    cells = base["cells"]
    expected = {f"{s}@{tf}" for s in cfg["symbols"] for tf in cfg["tf_list"]}
    assert set(cells) == expected and len(cells) == 15
    for cell in cells.values():
        assert set(cell) == set(rb.BASELINE_FIELDS)
        assert cell["E7_fired"] == cell["E7_long"] + cell["E7_short"]
        assert cell["E7_passed_f1"] <= cell["E7_fired"]
    assert base["totals"]["e1e6_short_1h"] == sum(
        c["E1_short"] + c["E6_short"] for k, c in cells.items()
        if k.endswith("@1h"))
    assert base["totals"]["E7_fired"] == sum(c["E7_fired"] for c in cells.values())
