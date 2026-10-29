"""接管层的语义契约(扩展层;策略链路零改动)。

钉的是三件"看着能跑、实盘会出事"的事:
① 失效价必须来自**结构**(最近支撑/阻力外沿 ∓ k_bm·ATR),而不是拍脑袋的百分比;
② 没有结构 / 结构太近(< k_is·ATR)⇒ 退回 Turtle 初始防线,绝不挂一个贴脸的废止损;
③ runner 侧把交易所既有仓位建档后**进入同一套持仓管理**:
   计入 `adopted` 而不是 `fills`、不发"开仓成交"、`_manage_stops` 会照常挂保护单。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from market_operator import adopt                                  # noqa: E402
import live_runner as lr                                           # noqa: E402


def _struct(atr=2.0, *, support=None, resistance=None) -> adopt.AdoptionStructure:
    zones = []
    if support is not None:
        zones.append(adopt.ZoneEdge("SUPPORT", inner=float(support[1]),
                                    outer=float(support[0])))
    if resistance is not None:
        zones.append(adopt.ZoneEdge("RESISTANCE", inner=float(resistance[0]),
                                    outer=float(resistance[1])))
    return adopt.AdoptionStructure(atr=atr, zones=tuple(zones))


# ---------------------------------------------------------------------------
# ① 结构锚点
# ---------------------------------------------------------------------------

def test_long_stop_anchored_to_support_outer_edge():
    """多头:失效 = 支撑外沿 − k_bm·ATR;目标 = 上方阻力内沿(VAH)。"""
    st = _struct(atr=2.0, support=(94.0, 96.0), resistance=(110.0, 113.0))
    ad = adopt.propose(symbol="BTCUSDT", direction=1, entry=100.0, mark=101.0,
                       structure=st)
    assert ad is not None
    assert ad.stop_source == "structure"
    assert ad.invalid == pytest.approx(94.0 - 0.1 * 2.0)
    assert ad.target_1 == pytest.approx(110.0)
    assert ad.target_source == "structure"
    assert ad.risk_atr == pytest.approx((100.0 - 93.8) / 2.0)
    assert ad.already_breached is False


def test_short_stop_anchored_to_resistance_outer_edge():
    """空头镜像:失效 = 阻力外沿 + k_bm·ATR;目标 = 下方支撑内沿(VAL)。"""
    st = _struct(atr=2.0, support=(90.0, 93.0), resistance=(104.0, 106.0))
    ad = adopt.propose(symbol="ETHUSDT", direction=-1, entry=100.0, mark=99.0,
                       structure=st)
    assert ad is not None
    assert ad.invalid == pytest.approx(106.0 + 0.1 * 2.0)
    assert ad.target_1 == pytest.approx(93.0)
    assert ad.stop_source == "structure"


# ---------------------------------------------------------------------------
# ② 无结构 / 结构太近 ⇒ Turtle 兜底(而不是挂贴脸止损)
# ---------------------------------------------------------------------------

def test_no_structure_falls_back_to_turtle_stop():
    ad = adopt.propose(symbol="SOLUSDT", direction=1, entry=50.0, mark=50.5,
                       structure=_struct(atr=1.0))
    assert ad is not None
    assert ad.stop_source == "atr_fallback"
    assert ad.invalid == pytest.approx(50.0 - 2.0 * 1.0)
    assert ad.target_1 is None and ad.target_source == "none"


def test_too_close_structure_falls_back_to_turtle_stop():
    """支撑离入场只有 0.3 ATR(< k_is=0.8)⇒ 该笔"距离不足",退回 Turtle。"""
    st = _struct(atr=2.0, support=(99.0, 99.5))
    ad = adopt.propose(symbol="BTCUSDT", direction=1, entry=100.0, mark=100.2,
                       structure=st)
    assert ad is not None
    assert ad.stop_source == "atr_fallback"
    assert ad.invalid == pytest.approx(100.0 - 2.0 * 2.0)


def test_degenerate_inputs_return_none():
    assert adopt.propose(symbol="X", direction=1, entry=0.0, mark=1.0,
                         structure=_struct()) is None
    assert adopt.propose(symbol="X", direction=1, entry=100.0, mark=100.0,
                         structure=_struct(atr=0.0)) is None
    with pytest.raises(ValueError):
        adopt.propose(symbol="X", direction=0, entry=100.0, mark=100.0,
                      structure=_struct())


def test_already_breached_flag_reported():
    """现价已越过失效价:仍给出接管参数,但如实标记 breached(由 §8.1 处置)。"""
    st = _struct(atr=2.0, support=(94.0, 96.0))
    ad = adopt.propose(symbol="BTCUSDT", direction=1, entry=100.0, mark=93.0,
                       structure=st)
    assert ad is not None and ad.already_breached is True


def test_structure_from_zones_maps_true_balance_bands():
    """SUPPORT 带 [lo_ext, VAL] / RESISTANCE 带 [VAH, hi_ext] 的映射方向。"""
    sup = types.SimpleNamespace(state="ACTIVE", role="SUPPORT", lo=94.0, hi=96.0)
    res = types.SimpleNamespace(state="ACTIVE", role="RESISTANCE", lo=110.0,
                                hi=113.0)
    dead = types.SimpleNamespace(state="DEAD", role="SUPPORT", lo=80.0, hi=81.0)
    st = adopt.structure_from_zones(atr=2.0, zones=[sup, res, dead])
    assert [(z.role, z.inner, z.outer) for z in st.zones] == [
        ("SUPPORT", 96.0, 94.0), ("RESISTANCE", 110.0, 113.0)]
    st2 = adopt.structure_from_zones(atr=2.0, zones=[sup, res, dead],
                                     k_bm=0.2, k_is=1.0, turtle_stop_atr=3.0)
    assert (st2.k_bm, st2.k_is, st2.turtle_stop_atr) == (0.2, 1.0, 3.0)


# ---------------------------------------------------------------------------
# ③ runner 侧接管:进入同一套持仓管理,但不伪装成开仓成交
# ---------------------------------------------------------------------------

class _TG:
    def __init__(self):
        self.alerts: list[str] = []
        self.fills: list[dict] = []

    def send_alert(self, msg, **_kw):
        self.alerts.append(str(msg))

    def send_fill(self, sym, title, qty, price, **kw):
        self.fills.append({"symbol": sym, "title": title, **kw})

    def send_daily(self, *a, **k):
        return True


class _Client:
    def __init__(self, positions=()):
        self._positions = list(positions)
        self.plan_placed: list[dict] = []

    def positions(self):
        return list(self._positions)

    def tpsl_orders_pending(self):
        return {"entrustedList": []}

    def place_stop_loss(self, symbol, **kw):
        self.plan_placed.append({"symbol": symbol, **kw})
        return {"orderId": f"P{len(self.plan_placed)}"}

    def cancel_tpsl_order(self, symbol, order_id):
        return {}


def _cfg() -> dict:
    cfg = json.loads((ROOT / "config" / "live.json").read_text("utf-8"))
    cfg["adopt_external_positions"] = {"enabled": True}
    return cfg


def _runner(tmp_path, client, tg, *, cfg=None):
    return lr.LiveRunner("live", cfg or _cfg(), client, tg,
                         state_path=tmp_path / "state.json",
                         journal_dir=tmp_path / "logs", contracts={},
                         symbols=())


def _campaign_stub(*, atr=2.0):
    sup = types.SimpleNamespace(state="ACTIVE", role="SUPPORT", lo=94.0, hi=96.0)
    res = types.SimpleNamespace(state="ACTIVE", role="RESISTANCE", lo=110.0,
                                hi=113.0)
    structure = types.SimpleNamespace(atr=lambda: atr, zones=[sup, res])
    opened: list[tuple] = []
    pm = types.SimpleNamespace(open=lambda *a, **k: opened.append((a, k)))
    return types.SimpleNamespace(tf_label="12h",
                                 detector=types.SimpleNamespace(
                                     structure=structure),
                                 pm=pm, cog_bars=[], trig_bars=[]), opened


def test_runner_adopts_exchange_only_position_with_structure_stop(tmp_path,
                                                                 monkeypatch):
    client = _Client([{"symbol": "BEUSDT", "direction": 1, "qty": 0.05,
                       "entry_price": 100.0, "mark_price": 101.0,
                       "unrealized_pl": 0.0, "leverage": 20.0}])
    tg = _TG()
    r = _runner(tmp_path, client, tg)
    cs, opened = _campaign_stub()
    monkeypatch.setattr(r, "_get_cs_for", lambda sym, tf=None: cs)

    r._adopt_exchange_only(client.positions())

    pos = r.positions["BEUSDT"]
    assert pos["direction"] == 1 and pos["qty"] == pytest.approx(0.05)
    assert pos["entry"] == pytest.approx(100.0)
    assert pos["invalid"] == pytest.approx(94.0 - 0.1 * 2.0)   # 结构外沿锚点
    assert pos["target_1"] == pytest.approx(110.0)
    assert pos["event_id"] == "ADOPTED"
    assert r.counters["adopted"] == 1
    assert r.counters["fills"] == 0                 # 不伪装成开仓成交
    assert tg.fills == [] and any("接管" in a for a in tg.alerts)
    assert opened, "接管后必须进入 PositionManager(结构出场才生效)"


def test_runner_adoption_disabled_keeps_old_behaviour(tmp_path, monkeypatch):
    cfg = _cfg()
    cfg["adopt_external_positions"] = {"enabled": False}
    client = _Client([{"symbol": "BEUSDT", "direction": 1, "qty": 0.05,
                       "entry_price": 100.0, "mark_price": 101.0}])
    r = _runner(tmp_path, client, _TG(), cfg=cfg)
    monkeypatch.setattr(r, "_get_cs_for",
                        lambda sym, tf=None: _campaign_stub()[0])

    r._adopt_exchange_only(client.positions())
    assert "BEUSDT" not in r.positions and r.counters.get("adopted", 0) == 0


def test_runner_adoption_without_campaign_alerts_and_skips(tmp_path, monkeypatch):
    """不在宇宙/无战役:不接管,但必须留下"为什么没接管"的痕迹。"""
    client = _Client([{"symbol": "ZZZUSDT", "direction": -1, "qty": 1.0,
                       "entry_price": 10.0, "mark_price": 10.1}])
    tg = _TG()
    r = _runner(tmp_path, client, tg)
    monkeypatch.setattr(r, "_get_cs_for", lambda sym, tf=None: None)
    r.campaigns.clear()

    r._adopt_exchange_only(client.positions())
    assert "ZZZUSDT" not in r.positions
    assert any("无法接管" in a for a in tg.alerts)


def test_reconcile_adopts_before_reporting_mismatch(tmp_path, monkeypatch):
    """对账顺序:先接管,后对账 ⇒ 接管成功的仓位不再报成 exchange_only。"""
    client = _Client([{"symbol": "BEUSDT", "direction": 1, "qty": 0.05,
                       "entry_price": 100.0, "mark_price": 101.0}])
    tg = _TG()
    r = _runner(tmp_path, client, tg)
    monkeypatch.setattr(r, "_get_cs_for",
                        lambda sym, tf=None: _campaign_stub()[0])

    r._reconcile()

    assert "BEUSDT" in r.positions
    assert not any("exchange_only" in a for a in tg.alerts)

