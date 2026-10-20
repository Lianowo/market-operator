"""v13 测试(docs/v13-protocol.md,冻结):级联触发尺度(全 TF 覆盖)。

覆盖(research-log #26 预注册;场景构造沿用 tests/test_v10_trigger.py 的
确定性价格路径,trigger 帧由逐位可重聚合的合成帧给出 —— 测试内硬断言):
- 级联映射表正确性:config/v13.json trigger_tf_map == trigger.TRIGGER_TF_MAP
  == 冻结映射(1h→15m/2h→30m/4h→1h/12h→2h/1d→4h);config parent=canonical
  数值先验逐项一致;enable_mic 默认 false(默认行为不激变);
- 映射解析:全档精确命中(声明=None);帧不可用/映射缺失 ⇒ 最近可用帧 +
  如实声明;无候选 ⇒ raise;
- 30m 聚合真实数据:逐位 == 15m 2:1 重算;30m 2:1 → 1h == 盘上 1h(锚定);
  validate 零缺口;manifest 条目 sha256 == 文件字节、源流/锚定记录齐全;
- 各 obs TF 的 mic 超前量(合成确定性):2h→30m E3 早 60 分钟、4h→1h 早 120
  分钟、1d→4h 早 960 分钟(E3 于第 2 根 trigger 揭示,N_f_mic 窗内);
- 数量闸门换算:2h→30m N_a_mic = 12 恰界发射/断于第 12 根不发射(bar 流
  同判);1d→4h scale=6 ⇒ MicTracker N_a_mic/N_f_mic = 18;恰 18 根站稳发射;
- parity(cascade):mic 帧 bar 事件流 == prepare_cognition 逐字段(2h→30m
  与 1d→4h)—— 检测器零改动硬约束;run_campaigns(enable_mic=False) 挂不挂
  级联 mic 帧逐位一致(bar 对照臂逐格同 canonical 的引擎级钉死);
- 引擎接线(cascade):2h 战役 + 30m trigger 注入 mic E1 ⇒ 次一 30m 开盘成交
  (decision=30m 收盘,fill−decision=1800s,advance vs 认知收盘 = 60 分钟),
  同现象 bar 事件认知收盘被槽位去重(恰一仓);确定性双跑(认知 + 引擎)。
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from market_operator import aggregate as agg
from market_operator import data as mo_data
from market_operator.backtest_v6 import SymbolCognition, prepare_cognition
from market_operator.broker import ContractSpec
from market_operator.campaign import (
    build_campaign_frame,
    campaign_key,
    run_campaigns,
)
from market_operator.events import (
    E1, E3, MarketEvent,
)
from market_operator.hypothesis import StructureView
from market_operator.structure import Params, Regime
from market_operator.trigger import (
    MicCognition,
    MicTracker,
    TRIGGER_TF_MAP,
    prepare_mic_cognition,
    resolve_trigger_tf,
)

T0 = 1_704_067_200_000                 # 2024-01-01 00:00:00 UTC(对齐全网格)
H = 3_600_000
M30 = 1_800_000
M15 = 900_000
P2 = Params(atr_n=2)                   # 编排用小窗口(同 test_v10 惯例)
SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

OBS_CASES = (                            # (obs_step, trig_step, tf_label)
    (2 * H, M30, "2h"),
    (4 * H, H, "4h"),
    (24 * H, 4 * H, "1d"),
)


def _mk_at(step):
    def _mk(bars, o, h, l, c):
        bars.append((T0 + step * len(bars), o, h, l, c, 1.0))
    return _mk


def _scenario(step):
    """以 step 步长重建 v10 的确定性价格路径(obs 尺度不变,时长随尺度)。"""

    def calm(bars, px=100.0, w=0.05, n=1):
        for _ in range(n):
            _mk_at(step)(bars, px, px + w, px - w, px)

    def seg(bars, *closes, w=0.03):
        for c in closes:
            o = bars[-1][4]
            _mk_at(step)(bars, o, max(o, c) + w, min(o, c) - w, c)

    def bar(bars, o, h, l, c):
        _mk_at(step)(bars, o, h, l, c)

    def h1_prefix(bars):
        calm(bars, 100.0, 0.05, 6)
        seg(bars, 100.95, 100.3)      # 6,7;H1 pivot:forming=6,known=7

    def e3_bars():
        bars = []
        h1_prefix(bars)
        seg(bars, 100.0)                            # 8
        bar(bars, 100.0, 100.78, 99.82, 100.75)     # 9:触碰,ex=100.78
        bar(bars, 100.75, 100.84, 100.72, 100.8)    # 10:ex→100.84
        bar(bars, 100.8, 100.96, 100.76, 100.93)    # 11:ex→100.96
        bar(bars, 100.93, 100.96, 100.6, 100.7)     # 12:回退 ⇒ E3 @12
        return bars

    def e1_bars():
        bars = []
        h1_prefix(bars)
        seg(bars, 100.6, 101.3)    # 8;9 = 破位候选
        seg(bars, 101.2, 101.25)   # 10;11 = 第 2、3 根 ⇒ E1 @11
        return bars

    return calm, seg, bar, h1_prefix, e3_bars, e1_bars


def trig_of(cog_bars, trig_step, reveal=3, carry_at=None):
    """认知帧(等步长)→ trigger 帧:reveal=k ⇒ (h,l)/close 于第 k 根揭示。

    聚合(obs:trig = ratio:1)open/high/low/close 逐位回认知帧 —— 由
    _assert_reaggregate 钉死(两帧描述同一市场)。
    """
    obs_step = cog_bars[1][0] - cog_bars[0][0]
    ratio = obs_step // trig_step
    assert obs_step % trig_step == 0 and ratio >= 2
    out = []
    for i, (ts, o, h, l, c, v) in enumerate(cog_bars):
        k = (carry_at or {}).get(i, reveal)
        closes = [o] * k + [c] * (ratio - k)
        opens = [o] + closes[:-1]
        for j in range(ratio):
            hj = h if j == k else max(opens[j], closes[j])
            lj = l if j == k else min(opens[j], closes[j])
            out.append((ts + trig_step * j, opens[j], hj, lj, closes[j], v))
    return out


def _assert_reaggregate(cog_bars, trig_bars, ratio):
    assert len(trig_bars) == ratio * len(cog_bars)
    for i, (ts, o, h, l, c, _v) in enumerate(cog_bars):
        q = trig_bars[ratio * i:ratio * i + ratio]
        assert q[0][0] == ts and q[0][1] == o
        assert max(x[2] for x in q) == h
        assert min(x[3] for x in q) == l
        assert q[-1][4] == c


def prep_cascade(cog_bars, trig_bars, obs_seconds, trig_seconds,
                 params=P2, **kw):
    obs_step = cog_bars[1][0] - cog_bars[0][0]
    assert obs_step == obs_seconds * 1000
    _assert_reaggregate(cog_bars, trig_bars, obs_seconds // trig_seconds)
    return prepare_mic_cognition(cog_bars, trig_bars, params, "SYN",
                                 obs_tf_seconds=obs_seconds,
                                 trigger_tf_seconds=trig_seconds, **kw)


def all_mic(mic, eid=None):
    return [e for _a, evs in sorted(mic.events_by.items())
            for e in evs if eid is None or e.id == eid]


def bar_stream(cog_bars, params=P2):
    return prepare_cognition(pd.DataFrame(
        cog_bars, columns=["ts", "open", "high", "low", "close", "volume"]),
        params, "SYN", enable_e7=True)


def _sig(e):
    z = e.attrs.get("zone")
    return (e.id, e.direction, e.known_index, e.ts,
            e.attrs.get("ref"), e.attrs.get("invalid"),
            e.attrs.get("started"), e.attrs.get("ex"),
            e.attrs.get("puncture_index"), e.attrs.get("from_role"),
            (z.lo, z.hi, str(z.role)) if z is not None else None)


# ---------------------------------------------------------------------------
# 1. 级联映射表正确性(config 冻结 + parent=canonical 数值先验一致)
# ---------------------------------------------------------------------------


def test_v13_map_frozen_and_config_pinning():
    frozen_map = {3600: 900, 7200: 1800, 14400: 3600, 43200: 7200,
                  86400: 14400}
    assert TRIGGER_TF_MAP == frozen_map
    cfg = json.loads(open("config/v13.json", encoding="utf-8").read())
    tf_map = {int(k): int(v) for k, v in cfg["trigger_tf_map"].items()}
    assert tf_map == frozen_map == TRIGGER_TF_MAP
    assert cfg["enable_mic"] is False, "默认不激变:bar 对照臂为默认"
    # scale 派生:数量闸门 = N × (obs/trigger);12h/1d 为协议 §1 声明档(6:1)
    assert {obs: obs // t for obs, t in frozen_map.items()} == {
        3600: 4, 7200: 4, 14400: 4, 43200: 6, 86400: 6}


def test_v13_config_priors_equal_canonical():
    v13 = json.loads(open("config/v13.json", encoding="utf-8").read())
    can = json.loads(open("config/canonical.json", encoding="utf-8").read())
    skip = {"version", "note", "meta",
            "whitelist_note", "merge_note",      # 叙述性注记(非数值先验)
            "enable_mic", "trigger_tf_map"}      # v13 新增机制开关
    for k in set(can) | set(v13):
        if k in skip:
            continue
        assert v13.get(k) == can.get(k), \
            f"v13 parent=canonical 数值先验必须逐项一致:{k} 分叉"
    assert v13["meta"]["parent"] == "config/canonical.json"


# ---------------------------------------------------------------------------
# 2. 映射解析(精确命中;fallback + 声明;无候选 raise)
# ---------------------------------------------------------------------------


def test_v13_resolver_exact_all_tiers():
    expect = {3600: 900, 7200: 1800, 14400: 3600, 43200: 7200, 86400: 14400}
    for obs, trig in expect.items():
        got, decl = resolve_trigger_tf(obs)
        assert (got, decl) == (trig, None), f"obs={obs} 应精确命中,无声明"


def test_v13_resolver_fallback_declares_and_divides():
    # 30m 不可用 ⇒ 2h obs 回落最近可用(1h),且必须为 obs 的除数(整 scale)
    got, decl = resolve_trigger_tf(7200, available=(900, 3600))
    assert got == 3600 and 7200 % got == 0
    assert isinstance(decl, str) and decl, "fallback 必须如实声明"
    # 映射缺失(obs=3h,下游目标 obs/4=45m 不在现有帧)⇒ 最近可除帧 + 声明
    got3, decl3 = resolve_trigger_tf(10_800,
                                     available=(900, 1800, 3600, 7200, 14400))
    assert 10_800 % got3 == 0 and got3 == 3600 and decl3
    # 无候选(可用帧都不整除或不小于 obs)⇒ raise(不得静默退 bar)
    with pytest.raises(RuntimeError):
        resolve_trigger_tf(1200, available=(900, 1800))
    with pytest.raises(RuntimeError):
        resolve_trigger_tf(3600, available=(7200, 14400))


# ---------------------------------------------------------------------------
# 3. 30m 聚合正确性 / 哈希(真实数据;manifest 入链)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sym", SYMS)
def test_v13_30m_exact_reaggregation_and_anchor(sym):
    df_15m = mo_data.load(sym, "15m")
    df_30m = mo_data.load(sym, "30m")
    df_1h = mo_data.load(sym)
    # 盘上 30m == 15m 2:1 逐位重算(字节级比对:read_csv 快速解析器个别
    # 1-ULP 舍入差异不影响确定性,序列化走同一实现 ⇒ 字节即"逐位"口径)
    recomputed = agg.aggregate_to_step(df_15m, agg.MS_30M)
    assert agg._csv_bytes(recomputed) == \
        open(f"data/raw/{sym}_30m.csv", "rb").read()
    # 锚定 1h:30m 2:1 → 1h == 盘上 1h(OHLC + ts 逐位;volume 为浮点和,
    # 用 approx —— 与 run_backtest_v10 对账同一口径)
    compact = agg.aggregate_to_step(df_30m, 3_600_000)
    for c in ("open", "high", "low", "close"):
        assert (compact[c].to_numpy() == df_1h[c].to_numpy()).all(), c
    assert (compact["ts"].to_numpy() == df_1h["ts"].to_numpy()).all()
    # 网格零缺口/零违例;体量
    assert mo_data.validate(df_30m, step_ms=agg.MS_30M) == []
    assert len(df_30m) == 2 * len(df_1h)
    assert df_30m["volume"].sum() == pytest.approx(df_15m["volume"].sum())
    assert df_30m.iloc[0]["open"] == df_15m.iloc[0]["open"]
    assert df_30m.iloc[-1]["close"] == df_15m.iloc[-1]["close"]


@pytest.mark.parametrize("sym", SYMS)
def test_v13_30m_manifest_hash_and_provenance(sym):
    manifest = json.loads(open("data/manifest.json", encoding="utf-8").read())
    rec = manifest["files"][f"{sym}_30m.csv"]
    raw = open(f"data/raw/{sym}_30m.csv", "rb").read()
    assert rec["sha256"] == hashlib.sha256(raw).hexdigest()
    assert rec["rows"] == len(raw.decode("utf-8").strip().splitlines()) - 1
    src = rec["aggregated_from"]
    assert src["file"] == f"{sym}_15m.csv"
    sha15 = hashlib.sha256(
        open(f"data/raw/{sym}_15m.csv", "rb").read()).hexdigest()
    assert src["sha256"] == sha15
    anchor = rec["anchored_to_1h"]
    sha1h = hashlib.sha256(
        open(f"data/raw/{sym}_1h.csv", "rb").read()).hexdigest()
    assert anchor["sha256"] == sha1h and rec["validation_issues"] == []
    assert rec["ts_last"] > rec["ts_first"]


def test_v13_30m_builder_idempotent_and_anchor_guard(tmp_path):
    """tmp 树重放构建:字节幂等;上游错位 ⇒ 锚定断言拒绝落盘。"""
    import pathlib
    raw = tmp_path / "raw"
    raw.mkdir()
    df_15m = pd.DataFrame(
        [(T0 + k * M15, 100.0 + (k % 4), 101.0 + (k % 4),
          99.0 + (k % 4), 100.5 + ((k + 1) % 4), 10.0) for k in range(16)],
        columns=mo_data.COLUMNS)
    # 1h 锚:同一市场(逐位与 15m 聚合一致)
    df_1h = agg.aggregate_to_step(df_15m, 3_600_000)
    df_15m.to_csv(raw / "SYN_15m.csv", index=False)
    df_1h.to_csv(raw / "SYN_1h.csv", index=False)
    mpath = tmp_path / "manifest.json"
    p1 = agg.aggregate_symbol_30m("SYN", data_dir=tmp_path,
                                  manifest_path=mpath)
    b1 = pathlib.Path(p1).read_bytes()
    p2 = agg.aggregate_symbol_30m("SYN", data_dir=tmp_path,
                                  manifest_path=mpath)
    assert pathlib.Path(p2).read_bytes() == b1, "同输入同字节(幂等)"
    rec = json.loads(mpath.read_text(encoding="utf-8"))["files"]["SYN_30m.csv"]
    assert rec["sha256"] == hashlib.sha256(b1).hexdigest()
    # 锚定破坏:1h 盘上帧漂移 ⇒ 拒绝落盘(对账先于写盘)
    df_bad = df_1h.copy()
    df_bad.loc[0, "close"] = df_bad.loc[0, "close"] + 1.0
    df_bad.to_csv(raw / "SYN_1h.csv", index=False)
    with pytest.raises(RuntimeError, match="锚定"):
        agg.aggregate_symbol_30m("SYN", data_dir=tmp_path,
                                 manifest_path=mpath)


# ---------------------------------------------------------------------------
# 4. 各 obs TF 的 mic 超前量(合成确定性;协议 §1 全 TF 覆盖)
# ---------------------------------------------------------------------------

_E3_REVEAL = 1            # 回退信息于第 2 根 trigger bar 收盘揭示


@pytest.mark.parametrize("obs_step,trig_step,tf", OBS_CASES)
def test_v13_e3_advance_minutes_per_obs_tf(obs_step, trig_step, tf):
    _calm, _seg, _bar, _h1, e3_bars, _e1 = _scenario(obs_step)
    cog_bars = e3_bars()
    ratio = obs_step // trig_step
    trig = trig_of(cog_bars, trig_step, reveal=_E3_REVEAL)
    mic = prep_cascade(cog_bars, trig, obs_step // 1000, trig_step // 1000)
    ref = bar_stream(cog_bars)
    mic_e3 = all_mic(mic, E3)
    bar_e3 = [e for evs in ref.events_by.values() for e in evs if e.id == E3]
    assert len(bar_e3) == 1 and len(mic_e3) == 1, tf
    e, b = mic_e3[0], bar_e3[0]
    a_expect = ratio * 12 + _E3_REVEAL            # E3 认知 bar = 12
    assert e.known_index == a_expect
    assert e.attrs["mic"] is True
    assert e.attrs["trigger_tf"] == trig_step // 1000
    assert e.attrs["ref"] == pytest.approx(b.attrs["ref"])
    # 阈值 = 最近已收认知 bar 的 ATR(结构锁定)
    assert e.attrs["invalid"] == pytest.approx(
        e.attrs["ref"] - e.direction * P2.k_bm * ref.atrs[11])
    # 超前量 = (obs − 2×trig) 分钟 = (ratio−2)×trig
    cog_close = T0 + obs_step * 13
    trig_close = T0 + trig_step * (a_expect + 1)
    advance_min = (cog_close - trig_close) / 60_000
    assert advance_min == (ratio - 2) * (trig_step // 60_000)
    assert advance_min == {"2h": 60, "4h": 120, "1d": 960}[tf]


# ---------------------------------------------------------------------------
# 5. 数量闸门尺度换算(2h→30m 恰界;1d→4h scale=6)
# ---------------------------------------------------------------------------


def test_v13_e1_counting_gate_2h_30m_exactly_12():
    _calm, _seg, _bar, _h1, _e3, e1_bars = _scenario(2 * H)
    cog_bars = e1_bars()
    ref = bar_stream(cog_bars)
    bar_e1 = [e for evs in ref.events_by.values() for e in evs if e.id == E1]
    assert len(bar_e1) == 1 and bar_e1[0].known_index == 11
    trig = trig_of(cog_bars, M30, reveal=0)       # 破位于首根 30m 揭示
    mic = prep_cascade(cog_bars, trig, 7200, 1800)
    e1s = all_mic(mic, E1)
    assert len(e1s) == 1
    e = e1s[0]
    assert e.attrs["started"] == 4 * 9 == 36      # 候选 = 破位 trigger 第 1 根
    assert e.known_index == 36 + 11 == 47         # 恰第 12 根(3×4)外侧收盘
    assert e.attrs["invalid"] == pytest.approx(
        e.attrs["zone"].lo - P2.k_bm * ref.atrs[10])   # 语境 ATR 锁定认知帧
    # 站稳 12 根 ≡ 3 认知 bar 全程 ⇒ 提前 0(同 v10 上界语义)
    assert (T0 + 2 * H * 12) - (T0 + M30 * 48) == 0

    # 对照:第 12 根收盘失守 ⇒ 不得发射(bar 流同样无 E1)
    ctrl = []
    _calm(ctrl, 100.0, 0.05, 6)
    _seg(ctrl, 100.95, 100.3)
    _seg(ctrl, 100.6, 101.3)
    _seg(ctrl, 101.2)
    _bar(ctrl, 101.2, 101.25, 100.4, 100.5)       # 11:收盘跌回带内
    mic_c = prep_cascade(ctrl, trig_of(ctrl, M30, reveal=0), 7200, 1800)
    assert all_mic(mic_c, E1) == []
    ref_c = bar_stream(ctrl)
    assert [e for evs in ref_c.events_by.values() for e in evs
            if e.id == E1] == []


def test_v13_scale_1d_4h_n_gates():
    tr = MicTracker(P2, scale=6, trigger_tf_seconds=14_400,
                    obs_tf_seconds=86_400)
    assert tr.n_a_mic == P2.n_a * 6 == 18
    assert tr.n_f_mic == P2.n_f * 6 == 18
    # 恰 18 根连续外侧站稳发射(1d→4h,E1 @ trigger 6×9+17=71,提前 0)
    _calm, _seg, _bar, _h1, _e3, e1_bars = _scenario(24 * H)
    cog_bars = e1_bars()
    trig = trig_of(cog_bars, 4 * H, reveal=0)
    mic = prep_cascade(cog_bars, trig, 86_400, 14_400)
    e1s = all_mic(mic, E1)
    assert len(e1s) == 1
    assert e1s[0].attrs["started"] == 6 * 9 == 54
    assert e1s[0].known_index == 54 + 17 == 71
    assert e1s[0].attrs["trigger_tf"] == 14_400
    ref = bar_stream(cog_bars)
    assert e1s[0].attrs["invalid"] == pytest.approx(
        e1s[0].attrs["zone"].lo - P2.k_bm * ref.atrs[10])


# ---------------------------------------------------------------------------
# 6. parity(cascade):bar 事件流零改动(2h→30m 与 1d→4h)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("obs_step,trig_step,tf", OBS_CASES)
def test_v13_bar_stream_parity_strict_cascade(obs_step, trig_step, tf):
    _calm, _seg, _bar, _h1, e3_bars, e1_bars = _scenario(obs_step)
    cog_bars = e3_bars() + []
    more = []
    build = e1_bars
    off = obs_step * len(cog_bars)
    more += [(b[0] + off, *b[1:]) for b in build()]
    cog_bars += [(b[0] + off, *b[1:]) for b in build()]
    trig = trig_of(cog_bars, trig_step, reveal=3)  # 全"晚揭示":聚合仍逐位一致
    mic = prep_cascade(cog_bars, trig, obs_step // 1000, trig_step // 1000)
    ref = bar_stream(cog_bars)
    got = sorted(_sig(e) for i, evs in mic.bar_events_by.items() for e in evs)
    want = sorted(_sig(e) for i, evs in ref.events_by.items() for e in evs)
    assert got == want and len(want) >= 2, \
        f"{tf}:mic 帧的 bar 事件流必须与 prepare_cognition 逐字段一致"


# ---------------------------------------------------------------------------
# 7. 无前馈(级联;发射后扰动不改已发射事件流)
# ---------------------------------------------------------------------------


def test_v13_no_future_perturbation_cascade():
    _calm, _seg, _bar, _h1, e3_bars, _e1 = _scenario(2 * H)
    cog_bars = e3_bars()
    trig = trig_of(cog_bars, M30, reveal=1)
    base = prep_cascade(cog_bars, trig, 7200, 1800)
    seq = sorted((a, e.id, e.direction, e.ts, e.attrs.get("ref"),
                  e.attrs.get("invalid"), e.attrs.get("ex"))
                 for a, evs in base.events_by.items() for e in evs)
    first_a = min(a for a, *_ in seq)
    assert first_a == 4 * 12 + 1 == 49
    pert = [b if k <= 49 else (b[0], 13.7, 9900.0, 0.01, 55.5, b[5])
            for k, b in enumerate(trig)]
    mic_a = prepare_mic_cognition(cog_bars, pert, P2, "SYN",
                                  obs_tf_seconds=7200, trigger_tf_seconds=1800)
    seq_a = sorted((a, e.id, e.direction, e.ts, e.attrs.get("ref"),
                    e.attrs.get("invalid"), e.attrs.get("ex"))
                   for a, evs in mic_a.events_by.items() for e in evs)
    upto = [row for row in seq_a if row[0] <= 49]
    assert upto == [row for row in seq if row[0] <= 49], \
        "发射时刻之后的 trigger bar(同认知 bar 内更晚根)不得改变事件流"


# ---------------------------------------------------------------------------
# 8. 引擎接线(2h 战役 + 30m trigger):次一 trigger 开盘成交 + bar 去重
# ---------------------------------------------------------------------------

SPEC = ContractSpec(symbol="TEST", size_min=0.01, size_step=0.01,
                    size_place=2, price_place=2, min_trade_usdt=5.0)
KW = dict(segment="full", dev_frac=0.7, equity0=300.0, risk_frac=0.0075,
          leverage_cap=5.0, taker_fee=0.0006, maker_fee=0.0002,
          slippage_per_side=0.0002, funding_per_8h=0.0001,
          cost_multiplier=1.0, contracts={"TEST": SPEC},
          params=P2, n_fill=12, portfolio_cap_frac=0.03)
ATR = 2.0
VIEW2H = StructureView(regime=Regime.TRANSITION, zones=(), atr=ATR)


def _engine_frames_2h30m(with_mic):
    """2h 战役:exec=1h(16 根),trigger=30m(32 根);bar E1 认知 bar 2;
    mic(可选)注入同现象 trigger E1 @ a=9(cog 2 的第 2 根 30m)。"""
    n_trig = 32
    rows = []
    for i in range(n_trig):
        c = 96.5
        rows.append((T0 + i * M30, c, c + 0.2, c - 0.2, c, 1.0))
    df_exec = pd.DataFrame(
        [(T0 + i * H, rows[i * 2][1],
          max(r[2] for r in rows[i * 2:i * 2 + 2]),
          min(r[3] for r in rows[i * 2:i * 2 + 2]),
          rows[i * 2 + 1][4], 1.0) for i in range(16)],
        columns=["ts", "open", "high", "low", "close", "volume"])
    cog_bars = [(T0 + i * 2 * H,
                 df_exec.open[i * 2], max(df_exec.high[i * 2:i * 2 + 2]),
                 min(df_exec.low[i * 2:i * 2 + 2]), df_exec.close[i * 2 + 1],
                 1.0) for i in range(8)]
    bar_e1 = MarketEvent(id=E1, direction=+1, ts=T0 + 2 * 2 * H, known_index=2,
                         attrs={"ref": 95.0, "invalid": 94.0})
    cog = SymbolCognition(symbol="TEST", bars=cog_bars, atrs=[ATR] * 8,
                          events_by={2: [bar_e1]}, pivots_by={},
                          views={2: VIEW2H})
    mic = None
    if with_mic:
        mic_e1 = MarketEvent(id=E1, direction=+1, ts=T0 + 9 * M30,
                             known_index=9,
                             attrs={"ref": 95.0, "invalid": 94.0,
                                    "mic": True, "trigger_tf": 1800})
        mic = MicCognition(
            symbol="TEST", obs_tf_seconds=7200, trigger_tf_seconds=1800,
            scale=4, bars=rows, cog_index_of=[i // 4 for i in range(32)],
            atrs=[ATR] * 32, events_by={9: [mic_e1]}, views={9: VIEW2H},
            bar_events_by={2: [bar_e1]}, counters={"E1": 1})
    return build_campaign_frame(cog, df_exec, "2h", mic_cog=mic)


def test_engine_cascade_fill_next_trigger_open_and_bar_dup_skip():
    f = _engine_frames_2h30m(with_mic=True)
    res = run_campaigns({campaign_key("TEST", "2h"): f}, **dict(KW),
                        enable_mic=True)
    opens = [r for r in res.ledger if r["kind"] == "OPEN"]
    assert len(opens) == 1, "同现象 mic/bar 双发必须由槽位去重为恰一仓"
    row = opens[0]
    assert row["decision_ts"] == T0 + 9 * M30     # 决策 = 30m trigger 收盘
    assert row["ts"] == T0 + 10 * M30             # 成交 = 次一 30m 开盘
    assert row["ts"] - row["decision_ts"] == M30
    assert row["meta"]["mic"] is True
    assert row["meta"]["cog_index"] == 2
    # 提前量 vs 认知收盘 = 认知 bar 2 收盘(T0+6h)− 成交(T0+5h)= 60 分钟
    assert (T0 + 6 * H) - row["ts"] == 60 * 60_000
    # 同现象去重(mic 占槽在前):路由层 dup 或撮合层 DUP_POSITION 必有其一
    dup_router = res.counters.get("dup", 0)
    dup_fill = sum(1 for r in res.ledger
                   if r["kind"] == "SKIP" and r["reason_code"] == "DUP_POSITION")
    assert dup_router + dup_fill >= 1, "同现象 bar 事件必须被槽位去重"
    assert res.counters["opens_filled"] == 1
    assert res.counters["mic_go_events"] == 1


def _engine_frames_cog4h_trig2h(with_mic):
    """4h 战役:exec=1h(16 根),trigger=2h(8 根)—— trigger 粗于执行帧。

    mic(可选)注入 trigger E1 @ a=1(标签 T0+2h,收盘 T0+4h):因果正确成交 =
    次一 trigger bar(T0+4h);v13 修补前会被中间执行 bar(T0+3h)揪出 ⇒
    早于决策收盘成交(前馈)。bar 对照事件不注入(本测试只钉 mic 因果)。
    """
    n_trig = 8
    rows = []
    for i in range(n_trig):
        c = 96.5
        rows.append((T0 + i * 2 * H, c, c + 0.2, c - 0.2, c, 1.0))
    df_exec = pd.DataFrame(
        [(T0 + i * H, c, c + 0.2, c - 0.2, c, 1.0) for i in range(16)
         for c in [96.5]],
        columns=["ts", "open", "high", "low", "close", "volume"])
    cog_bars = [(T0 + i * 4 * H, 96.5, 96.7, 96.3, 96.5, 1.0) for i in range(4)]
    cog = SymbolCognition(symbol="TEST", bars=cog_bars, atrs=[ATR] * 4,
                          events_by={}, pivots_by={}, views={})
    mic = None
    if with_mic:
        mic_e1 = MarketEvent(id=E1, direction=+1, ts=T0 + 2 * H, known_index=1,
                             attrs={"ref": 95.0, "invalid": 94.0,
                                    "mic": True, "trigger_tf": 7200})
        mic = MicCognition(
            symbol="TEST", obs_tf_seconds=14_400, trigger_tf_seconds=7200,
            scale=2, bars=rows, cog_index_of=[i // 2 for i in range(8)],
            atrs=[ATR] * 8, events_by={1: [mic_e1]}, views={1: VIEW2H},
            bar_events_by={}, counters={"E1": 1})
    return build_campaign_frame(cog, df_exec, "4h", mic_cog=mic)


def test_engine_no_lookahead_when_trigger_coarser_than_exec():
    """级联核心因果:trigger 粗于执行帧 ⇒ mic 市价不得早于决策收盘成交。"""
    f = _engine_frames_cog4h_trig2h(with_mic=True)
    res = run_campaigns({campaign_key("TEST", "4h"): f}, **dict(KW),
                        enable_mic=True)
    opens = [r for r in res.ledger if r["kind"] == "OPEN"]
    assert len(opens) == 1
    row = opens[0]
    assert row["decision_ts"] == T0 + 2 * H       # 决策 = 2h trigger 标签
    assert row["ts"] >= row["decision_ts"] + 2 * H, \
        "成交必须晚至决策 trigger bar 收盘之后(不得被中间执行 bar 前馈)"
    assert row["ts"] == T0 + 4 * H                # 次一 trigger bar 开盘
    assert row["meta"]["mic"] is True
    assert row["meta"]["cog_index"] == 0
    assert res.counters["mic_go_events"] == 1


def test_engine_mic_limit_no_lookahead_when_trigger_coarser_than_exec():
    """级联挂单因果:trigger 粗于执行帧 ⇒ mic 限价不得在决策收盘前上窗。

    挂单武装于决策 trigger 标签(收盘 = 标签+2h);修补前中间 1h 执行 bar
    (T0+3h)就会消耗 wait_bars 并允许成交(前馈);修补后首个可成交时点 =
    次一 trigger bar(T0+4h)。市价路径由上一测试钉死,此处钉限价路径的
    broker 缝(非 mic 路径不再触碰/消耗 mic 挂单)。"""
    from market_operator.broker import Broker, RestingLimitOrder
    b = Broker(equity=300.0, risk_frac=0.0075, leverage_cap=5.0,
               taker_fee=0.0006, slippage_per_side=0.0002,
               funding_per_8h=0.0001, contracts={"TEST": SPEC}, maker_fee=0.0002,
               portfolio_stop_risk_frac=0.03)
    b.submit_limit(RestingLimitOrder(
        direction=+1, limit_price=96.0, armed_at_ts=T0 + 2 * H,
        expiry_ts=T0 + 2 * H + 12 * H, fill_after_ts=(T0 + 2 * H) - H,
        symbol="TEST", role="entry", invalid=94.0, target_1=None,
        event_id="E1", reason="mic|test",
        meta={"direction": 1, "mic": True}))
    # 非 mic 路径(执行帧)反复经过:不得超时、不得消耗、不得成交
    for k in range(1, 3):
        rows = b.on_15m_bar("TEST", T0 + 2 * H + k * H, 96.5, 97.0, 96.0, 96.5)
        assert rows == [], "执行帧路径不得触碰 mic 挂单"
    assert b.resting["TEST"][0].wait_bars == 0
    # mic 路径首个时点 = 次一 trigger bar(决策收盘):严格穿过 ⇒ 允许成交
    rows = b.on_15m_bar("TEST", T0 + 4 * H, 96.5, 97.0, 95.0, 96.5,
                        mic_only=True)
    assert [r["kind"] for r in rows] == ["LIMIT_FILL"]
    assert rows[0]["ts"] == T0 + 4 * H


def test_engine_parity_cascade_mic_disabled_regardless_of_attachment():
    kw = dict(KW)
    r1 = run_campaigns({campaign_key("TEST", "2h"): _engine_frames_2h30m(False)},
                       **kw, enable_mic=False)
    r2 = run_campaigns({campaign_key("TEST", "2h"): _engine_frames_2h30m(True)},
                       **kw, enable_mic=False)
    assert json.dumps(r1.ledger, sort_keys=True) \
        == json.dumps(r2.ledger, sort_keys=True), \
        "enable_mic=False ⇒ 挂载级联 mic 帧不得改变任何账目(bar 对照臂逐格同 canonical)"
    opens = [r for r in r1.ledger if r["kind"] == "OPEN"]
    assert len(opens) == 1
    assert opens[0]["decision_ts"] == T0 + 2 * 2 * H
    assert opens[0]["ts"] == T0 + 6 * H            # canonical:次一执行(1h)bar 开盘
    assert r1.counters["mic_go_events"] == 0


def test_engine_determinism_double_run_cascade():
    kw = dict(KW)
    r1 = run_campaigns({campaign_key("TEST", "2h"): _engine_frames_2h30m(True)},
                       **kw, enable_mic=True)
    r2 = run_campaigns({campaign_key("TEST", "2h"): _engine_frames_2h30m(True)},
                       **kw, enable_mic=True)
    assert json.dumps(r1.ledger, sort_keys=True) \
        == json.dumps(r2.ledger, sort_keys=True)
    assert json.dumps(r1.equity_rows) == json.dumps(r2.equity_rows)
    # 认知层双跑(2h→30m)
    _calm, _seg, _bar, _h1, e3_bars, _e1 = _scenario(2 * H)
    cog_bars = e3_bars()
    trig = trig_of(cog_bars, M30, reveal=1)
    m1 = prep_cascade(cog_bars, trig, 7200, 1800)
    m2 = prep_cascade(cog_bars, trig, 7200, 1800)
    def dump(m):
        return json.dumps(
            {a: [(e.id, e.direction, e.ts,
                  {k: v for k, v in e.attrs.items() if k != "zone"})
                 for e in evs] for a, evs in sorted(m.events_by.items())},
            sort_keys=True, default=str)
    assert dump(m1) == dump(m2)
