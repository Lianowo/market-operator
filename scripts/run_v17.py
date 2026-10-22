"""v17 指令变体(docs/v17-directive.md,2026-10-20 所有者三条指令;research-log
#32 预注册,先于运行):equity=1000 USDT、1h 交易下线(tf_list=[2h,4h,12h,1d],
1h 仅执行路径细帧 + 4h 战役 trigger 帧)、级联级联触发(obs→obs/4)启用,其余
语义沿 v16(三闸+顺风档+收割婚配+结构锚;检测/费用/容量比例零改动)。

双臂(测量变体,不是协议重开):
- arm-M:mic 全族(用户指令语义)——全部 8 事件允许在 trigger 收盘先知;
- arm-C:仅续涨族(BREAKOUT_ACCEPTED/STRUCTURE_BROKEN/FLIP_RETESTED)激 mic,fade(BREAKOUT_FAILED/LEVEL_REJECTED/EDGE_RECLAIMED)与 TREND_RESUMED/RANGE_ROTATED 归 bar 触发
  (历史证据侧)。两臂共用同一批 mic 挂载战役帧,唯一差异 = mic_families。

用法:  python scripts/run_v17.py
配置:  config/v17.json(parent=config/v16.json,逐键硬对账:equity/tf_list/
       arms/trigger 映射之外零漂移;v16_params 与 v16 深等)
产物:  reports/v17_arm-{M,C}.{json,md}
       reports/trades_v17_arm-{M,C}__{full,dev,test}[_cost1_5,_cost2_0].csv
       reports/trades_v17_probe300_arm-{M,C}__dev[_cost*].csv(sizing 探针)

口径与纪律:
- 判定锚(dev 主评):dev@1.5x 净期望与净账,逐 TF 净账;mic 效果对照 =
  arm-M − arm-C(续涨 mic 常量,仅 fade 触发方式之差)+ 逐家族微笔数/提前量;
  v16 在册数字(300 USDT,15 战役无 mic)只作跨宇宙背景参照,不参与裁决。
- parity 双锚(与 v16/v13 同型,不等即 raise 不写产物):① FROZEN_ERA 名单
  (8 档)探针(dev 三格,arm=None,enable_mic=False,equity=300)== v6 冻结
  产物逐格全等(代码零回归);② 现行 16 档名单 bar 回放 dev 三格 == v13 在册
  bar 臂逐格全等(engine 零回归)。探针帧 = 臂帧(canonical 语义由 v13 引擎
  钉测试钉死:enable_mic=False 时挂载 mic 帧数值不变)+ 1h bar 战役帧。
- 对账硬约束:mic 帧复放 bar 事件流 == prepare_cognition 逐字段(检测器零
  改动);认知事件计数 == capped-v2 基准逐格(probe_frames 15 战役齐全);
  30m 帧 manifest 哈希复核;trigger 帧装载沿用 v13 校验链(validate/对齐/
  compaction 守恒);12h→2h、1d→4h 为协议 §1 "obs/4 无现帧取最近可用"声明档,
  如实声明。
- sizing 差(指令①的机械效果):同臂同语义 dev 三格权益 300→1000,min_size
  拒绝率(open_intents 中 MIN_SIZE SKIP 占比)与成交数对照,如实量账——
  只算 dev(探针本质,不构成 test 曝光);v16 在册(300,15 战役)背景另列。
- test 一次:第 20 次评估曝光(log#32 预注册;双臂同一轮次同次跑齐 1/1.5/2x
  三格,此后 v17 只许前向);既往升级线逐条照报,样本量 <60 ⇒ INSUFFICIENT_DATA;
  不渲染任何转正叙述。
- 若 mic 仍无增(dev@1.5x 全族 − 续涨差 ≤ 0):指令维持,结局如实存证。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_backtest_v6 as rb                                     # noqa: E402
import run_backtest_v13 as rv13                                  # noqa: E402
import run_v16 as rv16                                           # noqa: E402
from market_operator import aggregate as agg                     # noqa: E402
from market_operator import data as mo_data                      # noqa: E402
from market_operator.backtest_v6 import (                        # noqa: E402
    alignment_issues_ci,
    compute_metrics,
    prepare_cognition,
)
from market_operator.broker import CONTRACTS_PATH                # noqa: E402
from market_operator.campaign import (                           # noqa: E402
    ArmConfig,
    STOP_FAMILY,
    build_campaign_frame,
    parse_key,
    run_campaigns,
)
from market_operator.harvest import HarvestParams, V16Params, V16Router  # noqa: E402
from market_operator.research import block_bootstrap_ci, split_boundary  # noqa: E402
from market_operator.strategy_v6 import V6Params                 # noqa: E402
from market_operator.structure import Params                     # noqa: E402
from market_operator.trigger import (                            # noqa: E402
    TRIGGER_TF_MAP,
    prepare_mic_cognition,
    resolve_trigger_tf,
)

CONFIG_V17 = ROOT / "config" / "v17.json"
CONFIG_V16 = ROOT / "config" / "v16.json"
BASELINE_PATH = ROOT / "config" / "canonical_baseline_counts.json"
FROZEN_TURTLE = ROOT / "reports" / "backtest_v6_turtle.json"
FROZEN_V13 = ROOT / "reports" / "backtest_v13_cascade.json"
V16_REPORT = ROOT / "reports" / "v16.json"
REPORTS = ROOT / "reports"

SEGMENTS = ("full", "dev", "test")
COST_MULTS = rv16.COST_MULTS
JUDGE_COST = rv16.JUDGE_COST
MIN_TRADES_TEST = rv16.MIN_TRADES_TEST
FROZEN_ERA_WHITELIST = rv16.FROZEN_ERA_WHITELIST
METRIC_EQ = rv16.METRIC_EQ
ARM_IDS = ("arm-M", "arm-C")
TF_LIST = ["2h", "4h", "12h", "1d"]
TF5 = ("1h", "2h", "4h", "12h", "1d")
EQUITY0 = 1000.0
PROBE_EQUITY = 300.0                       # sizing 探针(= v16 在册权益口径)
UPGRADE_LINE = ("既往升级线(v4-v8 v16 不变口径):test≥60 笔 ∧ 1.5x 净期望>0 ∧ "
                "2x PF>1 ⇒ 才谈 forward dry-run;否则 Research Only 维持")
DIRECTIVE_CLAUSE = ("测量变体非协议重开(docs/v17-directive.md):若 mic 仍无增"
                    "(dev@1.5x 全族 − 仅续涨差 ≤ 0),指令维持但如实存证。三闸/"
                    "收割婚配/费用/容量比例全不动;1h 仅从属帧(执行路径与触发);"
                    "报告不渲染转正叙述。")
TRIGGER_LABEL = {900: "15m", 1800: "30m", 3600: "1h", 7200: "2h", 14400: "4h"}


# ---------------------------------------------------------------------------
# 配置(parent=v16 逐键硬对账;1h 不在场断言)
# ---------------------------------------------------------------------------


def tf_of(tfl: list) -> None:
    """tf_list 冻结钉死:恰 ["2h","4h","12h","1d"] 且无 1h 战役。"""
    if list(tfl) != TF_LIST:
        raise RuntimeError(f"tf_list 必须恰为 {TF_LIST}(1h 下线): {tfl}")
    if "1h" in tfl:
        raise RuntimeError("1h 战役不允许在场(执行/触发从属帧除外)")


def assert_frames_no_1h(frames: dict) -> None:
    bad = [k for k in frames if parse_key(k)[1] == "1h"]
    if bad:
        raise RuntimeError(f"1h 战役不允许在场: {bad[:4]}")


def load_config() -> dict:
    v17 = json.loads(CONFIG_V17.read_text(encoding="utf-8"))
    v16 = json.loads(CONFIG_V16.read_text(encoding="utf-8"))
    allow_diff = {"version", "note", "meta", "arms", "tf_list", "arm",
                  "equity", "enable_mic", "trigger_tf_map", "merge_note",
                  "whitelist_note", "v16_params"}   # 数值面下方单独深检
    for k in set(v17) | set(v16):
        if k in allow_diff:
            continue
        if v17.get(k) != v16.get(k):
            raise RuntimeError(f"config/v17.json parent=v16 漂移 {k}: "
                               f"{v17.get(k)} != {v16.get(k)}")
    p17 = {k: v for k, v in v17["v16_params"].items() if k != "note"}
    p16 = {k: v for k, v in v16["v16_params"].items() if k != "note"}
    if p17 != p16:
        raise RuntimeError("config/v17.json v16_params 数值面 != config/v16.json"
                           "(三闸/收割婚配先验不许碰;note 为叙述)")
    tf_of(v17["tf_list"])
    if float(v17["equity"]) != EQUITY0:
        raise RuntimeError(f"config/v17.json equity 必须 {EQUITY0}(指令①): "
                           f"{v17['equity']}")
    tf_map = {int(k): int(v) for k, v in v17["trigger_tf_map"].items()}
    if tf_map != TRIGGER_TF_MAP:
        raise RuntimeError("config/v17.json trigger_tf_map != 冻结级联映射")
    if set(v17["arms"]) != set(ARM_IDS):
        raise RuntimeError("config/v17.json arms 必须恰为 arm-M/arm-C")
    for a in ARM_IDS:
        spec = v17["arms"][a]
        if not (spec["v16"] and spec["fill_model"] == "exchange_maker"
                and spec["c3_stop_entry"]):
            raise RuntimeError(f"config/v17.json {a} v16 合成开关漂移: {spec}")
    if v17["arms"]["arm-M"]["mic_family"] != "all":
        raise RuntimeError("arm-M 必须 mic 全族(mic_family=all)")
    if v17["arms"]["arm-C"]["mic_family"] != list(STOP_FAMILY):
        raise RuntimeError("arm-C 必须仅续涨族 mic(BREAKOUT_ACCEPTED/STRUCTURE_BROKEN/FLIP_RETESTED)")
    return v17


# ---------------------------------------------------------------------------
# 帧装载(4 TF mic 挂载臂帧 + 1h bar 探针帧;认知对象共享)
# ---------------------------------------------------------------------------


def prepare_frames_v17(raw_cfg: dict, params: Params) -> tuple:
    """arm_frames(臂运行,mic 挂载)× probe_frames(锚探针,= 臂帧 + 1h bar 帧)。

    认知缓存(SymbolCognition)每次只构建一次,臂帧与探针帧共享对象;
    探针回放走 enable_mic=False(挂载 mic 帧被确定性忽略,v13 引擎钉测试)。
    """
    arm_frames: dict = {}
    probe_frames: dict = {}
    declarations: list = []
    parity: dict = {}
    dev_frac = float(raw_cfg["dev_frac"])
    tf_map = {int(k): int(v) for k, v in raw_cfg["trigger_tf_map"].items()}
    for sym in raw_cfg["symbols"]:
        df_1h = mo_data.load(sym)
        df_15m = mo_data.load(sym, "15m")
        issues = mo_data.validate(df_15m, step_ms=900_000)
        if issues:
            raise RuntimeError(f"{sym} 15m validation: {issues[:5]}")
        align = mo_data.check_frame_alignment(df_15m, df_1h, step_fine_ms=900_000)
        if align:
            raise RuntimeError(f"{sym} 15m↔1h alignment: {align[:5]}")
        rv13._compact_equals_1h(df_15m, df_1h, f"{sym} 15m")
        trig_cache: dict = {}
        for tf in TF5:
            t0 = time.time()
            if tf == "1h":
                df_cog, df_exec = df_1h, df_15m
            else:
                df_cog = agg.load_tf(sym, tf)
                df_exec = df_1h
                step = agg.TF_HOURS[tf] * 3_600_000
                issues_tf = mo_data.validate(df_cog, step_ms=step)
                if issues_tf:
                    raise RuntimeError(f"{sym} {tf} validation: {issues_tf[:5]}")
                expected = agg.aggregate(df_1h, agg.TF_HOURS[tf])
                ok = all((expected[c] - df_cog[c]).abs().max() == 0.0
                         for c in ("open", "high", "low", "close"))
                if not ok or not (expected["ts"] == df_cog["ts"]).all():
                    raise RuntimeError(f"{sym} {tf} 盘上帧 != 1h 聚合重算(漂移)")
            cog = prepare_cognition(df_cog, params, sym, enable_e7=True)
            if tf == "1h":
                ci = alignment_issues_ci(cog.bars, [(int(r.ts),) for r in
                                                    df_15m.itertuples(index=False)],
                                         dev_frac)
                if ci:
                    raise RuntimeError(f"{sym} boundary alignment: {ci}")
                f = build_campaign_frame(cog, df_exec, tf)
                probe_frames[f.key] = f        # 1h 只作锚探针,不进臂宇宙
                n_ev = sum(len(v) for v in cog.events_by.values())
                print(f"[frame] {sym}@{tf}(探针 bar): {len(df_cog)} 认知 bar, "
                      f"{n_ev} events ({time.time() - t0:.1f}s)", flush=True)
                continue
            obs = agg.TF_HOURS[tf] * 3600
            trig_tf, decl = resolve_trigger_tf(obs, tf_map=tf_map)
            if decl is not None:
                declarations.append(f"{sym}@{tf}: {decl}")
            if trig_tf not in trig_cache:
                trig_cache[trig_tf] = rv13._load_trigger_frame(
                    sym, trig_tf, df_1h, df_15m)
            mic = prepare_mic_cognition(
                df_cog, trig_cache[trig_tf], params, sym,
                obs_tf_seconds=obs, trigger_tf_seconds=trig_tf, enable_e7=True)
            want = sorted((i, e.id, e.direction,
                           e.attrs.get("ref"), e.attrs.get("invalid"))
                          for i, evs in cog.events_by.items() for e in evs)
            got = sorted((i, e.id, e.direction,
                          e.attrs.get("ref"), e.attrs.get("invalid"))
                         for i, evs in mic.bar_events_by.items() for e in evs)
            if got != want:
                raise RuntimeError(
                    f"{sym}@{tf} mic 帧 bar 事件流 != prepare_cognition"
                    "(检测器改动?)")
            parity[f"{sym}@{tf}"] = sum(len(v) for v in
                                        mic.bar_events_by.values())
            f = build_campaign_frame(cog, df_exec, tf, mic_cog=mic)
            arm_frames[f.key] = f
            probe_frames[f.key] = f
            n_ev = sum(len(v) for v in cog.events_by.values())
            n_mic = sum(len(v) for v in mic.events_by.values())
            print(f"[frame] {sym}@{tf}(mic {TRIGGER_LABEL.get(trig_tf)}): "
                  f"{len(df_cog)} 认知 bar, {n_ev} bar / {n_mic} mic events "
                  f"({time.time() - t0:.1f}s)", flush=True)
    assert_frames_no_1h(arm_frames)
    return arm_frames, probe_frames, {"declarations": declarations,
                                      "bar_parity": parity}


# ---------------------------------------------------------------------------
# 臂定义 + 回放驱动
# ---------------------------------------------------------------------------


def _v16_params(rp_base: V6Params, hp) -> V16Params:
    return V16Params(**{f: getattr(rp_base, f)
                        for f in V6Params.__dataclass_fields__}, v16=hp)


def build_arm(raw_cfg: dict, arm_id: str) -> tuple:
    """臂 → (HarvestParams, V16Router, ArmConfig, mic_families)。

    ArmConfig 与 v16 合成臂逐项同值(三闸/收割婚配/家族差分全不动);
    唯一差异 = mic_families(arm-M:None=全族;arm-C:STOP_FAMILY)。
    """
    p = raw_cfg["v16_params"]
    hp = HarvestParams(
        **{k: p[k] for k in ("pos_short_min", "pos_long_max", "k_room_R",
                             "k_is", "k_star", "tier_a_mult", "tier_b_mult",
                             "k_cap_hours", "cap_mfe_min")},
        fade_family=tuple(p["fade_family"]))
    arm = ArmConfig(fill_model="exchange_maker",
                    stop_entry=True,
                    stop_wait_tf=int(p["stop_wait_tf"]),
                    k_bm=float(p["k_bm"]),
                    c2_family=tuple(p["fade_family"]),
                    harvest_family=tuple(p["fade_family"]),
                    time_cap_bars=int(p["k_cap_hours"]),
                    cap_mfe_min=float(p["cap_mfe_min"]))
    mic_families = None if arm_id == "arm-M" else tuple(p["stop_family"])
    if arm_id == "arm-C" and mic_families != STOP_FAMILY:
        raise RuntimeError("arm-C mic 族必须恰为续涨族 BREAKOUT_ACCEPTED/STRUCTURE_BROKEN/FLIP_RETESTED")
    return hp, V16Router, arm, mic_families


def run_grid(frames: dict, raw_cfg: dict, params: Params, rp, factory, arm,
             contracts: dict, *, segment: str, equity0: float,
             mic_families=None, enable_mic: bool = True,
             mults: tuple = COST_MULTS, tag: str = "") -> dict:
    broker_kw = dict(dev_frac=float(raw_cfg["dev_frac"]), equity0=equity0,
                     risk_frac=float(raw_cfg["risk_frac"]),
                     leverage_cap=float(raw_cfg["leverage_cap"]),
                     taker_fee=float(raw_cfg["taker_fee"]),
                     maker_fee=float(raw_cfg["maker_fee"]),
                     slippage_per_side=float(raw_cfg["slippage_per_side"]),
                     funding_per_8h=float(raw_cfg["funding_per_8h_per_side"]),
                     contracts=contracts, params=params,
                     n_fill=int(raw_cfg["n_fill"]),
                     portfolio_cap_frac=float(raw_cfg["portfolio_cap_frac"]))
    out = {}
    for mult in mults:
        t0 = time.time()
        res = run_campaigns(frames, segment=segment, cost_multiplier=mult,
                            router_params=rp, router_factory=factory, arm=arm,
                            enable_mic=enable_mic, mic_families=mic_families,
                            **broker_kw)
        if enable_mic:
            res.trades = augment_trades_v17(res.trades, frames, res.ledger)
        m = compute_metrics(res)
        out[mult] = (res, m)
        c = res.counters
        print(f"      [bt] {tag}{segment}@{mult}x{' mic' if enable_mic else ' bar'}"
              f": net={m['net_pnl']:+.2f} n={m['n_trades']} "
              f"pf={m['profit_factor'] and round(m['profit_factor'], 3)} "
              f"min_skip={c.get('min_size_skips', 0)} "
              f"mic_filt={c.get('mic_family_filtered', 0)} "
              f"({time.time() - t0:.0f}s)", flush=True)
    liq = sum(res.counters["liquidations"] for res, _ in out.values())
    if liq != 0:
        raise RuntimeError(f"强平非预期触发: {liq}")
    return out


# ---------------------------------------------------------------------------
# v17 归因:dleague 逐 TF 净账 / 1d 出现数 / sized 差 / mic 家族对照
# ---------------------------------------------------------------------------


def signals_by_tf(frames: dict, dev_frac: float, *, segment: str) -> dict:
    """段内事件数(bar 发射 / mic 发射;miEvents 按认知 bar 归属段)。"""
    out = {tf: {"bar": 0, "mic": 0} for tf in TF_LIST}
    for key, f in frames.items():
        tf = f.tf_label
        if tf not in out:
            continue
        b = split_boundary(len(f.cog.bars), dev_frac)
        lo, hi = (0, b) if segment == "dev" else (b, len(f.cog.bars))
        for i in range(lo, hi):
            out[tf]["bar"] += len(f.cog.events_by.get(i, ()))
        if f.mic is not None:
            for a, evs in f.mic.events_by.items():
                if lo <= f.mic.cog_index_of[a] < hi:
                    out[tf]["mic"] += len(evs)
    return out


def tf_ledger(res, segment_signals: dict) -> dict:
    """逐 TF 账:信号(bar/mic)、三闸评估/拒、成交、净账、min_size、容量跳单。

    机读口径:成交/额 ← res.trades(段内开仓);min_size ← 账本 SKIP/MIN_SIZE;
    容量 ← res.cap_skips;三闸 ← counters["campaigns"] 路由计数聚合。
    """
    led = {tf: {"signals_bar": seg.get("bar", 0), "signals_mic": seg.get("mic", 0),
                "gate_evaluated": 0, "gate_reject_q1_pos": 0,
                "gate_reject_q2_room": 0, "gate_reject_f1": 0,
                "slot_dup": 0, "n_trades": 0, "net_pnl": 0.0,
                "min_size_skips": 0, "portfolio_cap_skips": 0}
           for tf, seg in segment_signals.items()}
    for t in res.trades:
        if not t["in_segment_open"]:
            continue
        c = led[t["tf"]]
        c["n_trades"] += 1
        c["net_pnl"] += float(t["net_pnl"])
    for row in res.ledger:
        if row["kind"] == "SKIP" and row["reason_code"] == "MIN_SIZE":
            led[parse_key(row["symbol"])[1]]["min_size_skips"] += 1
    for sk in res.cap_skips:
        led[parse_key(sk["campaign"])[1]]["portfolio_cap_skips"] += 1
    for key, c in res.counters["campaigns"].items():
        tf = parse_key(key)[1]
        g = c.get
        led[tf]["gate_evaluated"] += g("v16_gate_evaluated", 0)
        led[tf]["gate_reject_q1_pos"] += g("v16_gate_reject_q1_pos", 0)
        led[tf]["gate_reject_q2_room"] += g("v16_gate_reject_q2_room", 0)
        led[tf]["gate_reject_f1"] += g("v16_gate_reject_f1", 0)
        led[tf]["slot_dup"] += g("dup", 0)
    return led


def explain_1d(arm: str, seg: str, led_tf: dict) -> str:
    """1d 出现数机读归因(数字直译,不叙述)。"""
    c = led_tf["1d"]
    if c["n_trades"] > 0:
        return (f"{arm} {seg}:1d 成交 {c['n_trades']} 笔(净 "
                f"{c['net_pnl']:+.2f});信号 bar={c['signals_bar']}/"
                f"mic={c['signals_mic']}(trigger 4h 声明档)。")
    if c["signals_bar"] == 0 and c["signals_mic"] == 0:
        return (f"{arm} {seg}:1d 成交 0 —— 段内无事件发射(bar/mic 均为 0;"
                "结构条件未触发,非闸线问题)。")
    rej = (f"三闸拒 q1={c['gate_reject_q1_pos']}/q2={c['gate_reject_q2_room']}/"
           f"f1={c['gate_reject_f1']}")
    return (f"{arm} {seg}:1d 成交 0 —— 信号 bar={c['signals_bar']}/"
            f"mic={c['signals_mic']},闸评估={c['gate_evaluated']},{rej},"
            f"槽位占{d_gone(d=c['slot_dup'])}min_size={c['min_size_skips']},"
            f"容量跳={c['portfolio_cap_skips']};未得成交窗口。")


def d_gone(d):
    return f"dup={d}," if d else ""


def sizing_row(res) -> dict:
    c = res.counters
    intents = c.get("open_intents", 0)
    skips = c.get("min_size_skips", 0)
    return {"open_intents": intents, "min_size_skips": skips,
            "min_size_rate": (skips / intents) if intents else None,
            "fills": c.get("opens_filled", 0) + c.get("limit_fills_entry", 0)}


def mic_entry_share(trades: list) -> dict:
    """成交入口 mic 占比(逐家族):mic=True 开交数 / 家族成交数。"""
    out: dict = {}
    for t in trades:
        if not t["in_segment_open"]:
            continue
        fam = rv16._family_of(t.get("event_id"))
        d = out.setdefault(fam, {"mic": 0, "bar": 0})
        d["mic" if t.get("mic") else "bar"] += 1
    return out


# ---------------------------------------------------------------------------
# 逐笔增广(v16 溯源列 + mic 提前分钟)
# ---------------------------------------------------------------------------


def augment_trades_v17(trades: list, frames: dict, ledger: list) -> list:
    aug = rv16.augment_trades_v16(trades, frames, ledger)
    meta_by_tid: dict = {}
    for row in ledger:
        if row["trade_id"] is None:
            continue
        if row["kind"] == "OPEN" or (row["kind"] == "LIMIT_FILL"
                                     and row["meta"].get("role") != "reduce"):
            meta_by_tid.setdefault(row["trade_id"], row["meta"])
    out = []
    for t in aug:
        m = meta_by_tid.get(t["trade_id"], {})
        is_mic = bool(m.get("mic"))
        adv = None
        if is_mic:
            f = frames[t["campaign"]]
            i = int(m.get("cog_index"))
            cog_close = int(f.cog.bars[i][0]) + f.tf_hours * 3_600_000
            adv = (cog_close - int(t["open_ts"])) / 60_000.0
        out.append({**t, "mic": is_mic, "cog_index": m.get("cog_index"),
                    "advance_minutes": adv})
    return out


COLUMNS_V17 = list(rv16.COLUMNS_V16) + ["mic", "cog_index", "advance_minutes"]


def trades_frame_v17(trades: list) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({**t,
                     "open_decision_iso": rb._iso(t["open_decision_ts"]),
                     "open_iso": rb._iso(t["open_ts"]),
                     "close_decision_iso": rb._iso(t["close_decision_ts"]),
                     "exit_iso": rb._iso(t["exit_ts"]),
                     "reason_reduce": " ;; ".join(t["reasons_reduce"])})
    return pd.DataFrame(rows, columns=COLUMNS_V17)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _cellm(m: dict) -> dict:
    return {"net_pnl": m["net_pnl"], "n_trades": m["n_trades"],
            "winrate": m["winrate"], "profit_factor": m["profit_factor"],
            "expectancy_usdt": m["expectancy_usdt"],
            "expectancy_r": m["expectancy_r"]}


def main() -> None:
    t_start = time.time()
    raw_cfg = load_config()
    params = Params.from_json(CONFIG_V17)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist"])
    rp_base = V6Params(k_dq=float(raw_cfg["k_dq"]),
                       k_imp=float(raw_cfg["k_imp"]), k_is=float(raw_cfg["k_is"]),
                       turtle_stop_atr=float(raw_cfg["turtle_stop_atr"]),
                       k_bm=float(raw_cfg["k_bm"]), whitelist=whitelist)
    if float(raw_cfg.get("merge_max_atr", 0.0)) != 2.0:
        raise RuntimeError("v17 parent=v16:merge_max_atr 必须 2.0")

    contracts, spec_src = rb.load_contracts_cache(symbols)
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    print(f"[contracts] source={spec_src} sha256={contracts_sha[:12]}…", flush=True)

    # -- 30m 帧幂等保障(manifest 哈希链,v13 纪律沿用) ------------------------------
    for sym in symbols:
        p30 = agg.ensure_30m([sym])[sym]
        rec = json.loads((ROOT / "data" / "manifest.json")
                         .read_text(encoding="utf-8"))["files"][f"{sym}_30m.csv"]
        assert hashlib.sha256(p30.read_bytes()).hexdigest() == rec["sha256"]
    print("[data] 30m 帧齐(manifest 哈希复核过)", flush=True)

    # -- 帧(4 TF mic 挂载臂帧 + 1h bar 探针帧;认知对象共享) -------------------------
    print("[prep] v17 战役帧构建(认知一次性;追/探针共享)", flush=True)
    frames, probe_frames, prep = prepare_frames_v17(raw_cfg, params)
    for d in prep["declarations"]:
        print(f"[decl] {d}", flush=True)
    xcheck = rb.cross_checks(probe_frames, float(raw_cfg["k_is"]),
                             merge_max_atr=2.0)

    frozen_turtle = json.loads(FROZEN_TURTLE.read_text(encoding="utf-8"))
    frozen_v13 = json.loads(FROZEN_V13.read_text(encoding="utf-8"))
    v16_report = json.loads(V16_REPORT.read_text(encoding="utf-8"))
    if v16_report["config"]["equity0"] != PROBE_EQUITY:
        raise RuntimeError("v16 在册报告 equity0 != 300(背景引证校验)")

    # -- 锚①/②(探针回放:equity=300,mic 禁用,plain 路由) ----------------------------
    print("[anchor-1] FROZEN_ERA 探针(dev 三格,arm=None,mic 禁用)", flush=True)
    rp_probe = dc_replace(rp_base, whitelist=frozenset(FROZEN_ERA_WHITELIST))
    probe = run_grid(probe_frames, raw_cfg, params, rp_probe, None, None,
                     contracts, segment="dev", equity0=PROBE_EQUITY,
                     enable_mic=False, tag="anchor1 ")
    parity_probe = rv16.assert_anchor("FROZEN_ERA 探针", probe,
                                      frozen_turtle["runs"], "turtle|dev")
    print("[anchor-1] 探针 3 格 == v6 冻结产物(逐格全等)", flush=True)

    print("[anchor-2] 现行 16 档名单 bar 回放 == v13 在册 bar 臂", flush=True)
    probe16 = run_grid(probe_frames, raw_cfg, params, rp_base, None, None,
                       contracts, segment="dev", equity0=PROBE_EQUITY,
                       enable_mic=False, tag="anchor2 ")
    parity_bar16 = rv16.assert_anchor("16 档 bar 回放", probe16,
                                      frozen_v13["runs"], "bar|dev")
    print("[anchor-2] 16 档 3 格 == v13 在册 bar 臂(逐格全等)", flush=True)

    # -- 主网格:双臂 equity=1000,full/dev/test × 成本(9 格/臂) ----------------------
    grids: dict = {}
    arms_spec: dict = {}
    for arm_id in ARM_IDS:
        hp, factory, arm, mic_families = build_arm(raw_cfg, arm_id)
        arms_spec[arm_id] = {"hp": hp, "mic_families": mic_families}
        rp_use = _v16_params(rp_base, hp)
        print(f"[arm] {arm_id}: {raw_cfg['arms'][arm_id]['note']}", flush=True)
        grids[arm_id] = {}
        for seg in SEGMENTS:
            grids[arm_id][seg] = run_grid(
                frames, raw_cfg, params, rp_use, factory, arm, contracts,
                segment=seg, equity0=EQUITY0, mic_families=mic_families,
                enable_mic=True, tag=f"{arm_id} ")

    # -- sizing 探针:同臂同语义 dev 三格 equity=300(测量指令①的机械差;不算曝光) -----
    print("[probe300] sizing 探针(dev 三格,equity=300;min_size 拒绝率对照)",
          flush=True)
    probes300: dict = {}
    for arm_id in ARM_IDS:
        hp, factory, arm, mic_families = build_arm(raw_cfg, arm_id)
        rp_use = _v16_params(rp_base, hp)
        probes300[arm_id] = run_grid(
            frames, raw_cfg, params, rp_use, factory, arm, contracts,
            segment="dev", equity0=PROBE_EQUITY, mic_families=mic_families,
            enable_mic=True, tag=f"{arm_id}-p300 ")

    # -- 归因装配 ---------------------------------------------------------------------
    sig = {seg: signals_by_tf(frames, dev_frac, segment=seg)
           for seg in ("dev", "test")}
    full_sig = {tf: {"bar": sig["dev"][tf]["bar"] + sig["test"][tf]["bar"],
                     "mic": sig["dev"][tf]["mic"] + sig["test"][tf]["mic"]}
                for tf in TF_LIST}
    attr: dict = {}
    for arm_id in ARM_IDS:
        attr[arm_id] = {}
        for seg in SEGMENTS:
            # full 段信号网 = dev+test 合并(与段的 in_segment_open 归因一致)
            res_sig = full_sig if seg == "full" else sig[seg]
            for mult in COST_MULTS:
                res, m = grids[arm_id][seg][mult]
                cell = {
                    "metrics": m, "clause": rv16.clause_stats(res.counters),
                    "harvest": rv16.harvest_buckets(res.trades),
                    "family": rv16.family_buckets(res.trades),
                    "tf_ledger": tf_ledger(res, res_sig),
                    "mic_share": mic_entry_share(res.trades),
                    "advance": rv13.mic_advance_stats(res, frames),
                    "giveback": rb.giveback_summary(res.trades),
                    "buckets": rb.bucket_tables(res.trades),
                    "n_trades_1d": sum(1 for t in res.trades
                                       if t["in_segment_open"] and t["tf"] == "1d"),
                }
                attr[arm_id][f"{seg}@{mult}x"] = cell
        for mult in COST_MULTS:
            res300, m300 = probes300[arm_id][mult]
            attr[arm_id][f"probe300|dev@{mult}x"] = {
                "metrics": m300, "sizing": sizing_row(res300),
                "min_size_rate_1000": sizing_row(grids[arm_id]["dev"][mult][0]),
            }

    # -- 判定锚(dev@1.5x,mic 对照) ------------------------------------------------------
    def exp(a, seg, mult):
        return grids[a][seg][mult][1]["expectancy_usdt"]

    eM, eC = exp("arm-M", "dev", JUDGE_COST), exp("arm-C", "dev", JUDGE_COST)
    mic_delta = ((eM - eC) if eM is not None and eC is not None else None)
    directive_outcome = {
        "judge": ("dev@1.5x 净期望 arm-M − arm-C(fade 触发方式孤立差;"
                  "续涨 mic 两臂常量)"),
        "arm_M_expectancy_usdt": eM, "arm_C_expectancy_usdt": eC,
        "delta_expectancy_usdt": mic_delta,
        "delta_net_pnl": (grids["arm-M"]["dev"][JUDGE_COST][1]["net_pnl"]
                          - grids["arm-C"]["dev"][JUDGE_COST][1]["net_pnl"]),
        "outcome": ("mic 全族增益为正(如实存证,不构成转正叙述)"
                    if mic_delta is not None and mic_delta > 0
                    else "mic 无增 ⇒ 指令维持,结局如实存证(测量变体,非协议重开)"),
        "clause": DIRECTIVE_CLAUSE}

    # -- test 一次(第 20 次曝光) ----------------------------------------------------------
    test_once = {}
    for arm_id in ARM_IDS:
        res1, m1 = grids[arm_id]["test"][1.0]
        res15, m15 = grids[arm_id]["test"][1.5]
        _, m20 = grids[arm_id]["test"][2.0]
        tr_in = [t for t in res1.trades if t["in_segment_open"]]
        ci = (block_bootstrap_ci(
            np.asarray([t["net_pnl"] for t in tr_in], dtype=np.float64),
            np.asarray([t["open_ts"] for t in tr_in], dtype=np.int64))
            if tr_in else (float("nan"), float("nan")))
        up = {
            "n_trades_test_1x": m1["n_trades"],
            "sample_size": ("SUFFICIENT(>=60)" if m1["n_trades"] >= MIN_TRADES_TEST
                            else "INSUFFICIENT_DATA"),
            "expectancy_usdt_test_1_5x": m15["expectancy_usdt"],
            "expectancy_positive_1_5x": (m15["expectancy_usdt"] is not None
                                         and m15["expectancy_usdt"] > 0.0),
            "profit_factor_test_2_0x": m20["profit_factor"],
            "pf_gt_1_2_0x": (m20["profit_factor"] is not None
                             and m20["profit_factor"] > 1.0),
        }
        up["verdict"] = (
            "过线(可谈 forward dry-run)"
            if (up["sample_size"].startswith("SUFFICIENT")
                and up["expectancy_positive_1_5x"] and up["pf_gt_1_2_0x"])
            else "不过线(Research Only 维持)")
        test_once[arm_id] = {
            "test_1x": _cellm(m1), "test_1_5x": _cellm(m15),
            "test_2x": _cellm(m20), "dev_1x": _cellm(grids[arm_id]["dev"][1.0][1]),
            "dev_1_5x": _cellm(grids[arm_id]["dev"][1.5][1]),
            "dev_2x": _cellm(grids[arm_id]["dev"][2.0][1]),
            "test_net_pnl_mean_ci95_1x": list(ci), "upgrade_line": up}
    exposure_note = ("test 段第 20 次评估曝光(research-log #32 预注册,一次性;"
                     "双臂同一轮次同次跑齐 1/1.5/2x 三格;sizing 探针只落 dev,"
                     "不构成 test 曝光;v17 此后只许前向)")

    # -- v16 在册背景(跨宇宙参照,不参与裁决) ------------------------------------------------
    v16_bg = {k: _cellm(v16_report["runs"][k]["metrics"]) for k in
              ("v16|dev@1.5x", "arm0|dev@1.5x")}

    # -- 产物装配/落盘 ----------------------------------------------------------------------
    REPORTS.mkdir(exist_ok=True)
    common = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "doc": "docs/v17-directive.md(2026-10-20 所有者三条指令)",
            "pre_registration": "research-log #32(先于运行)",
            "directive_clause": DIRECTIVE_CLAUSE,
            "test_eval_declaration": exposure_note,
            "upgrade_line": UPGRADE_LINE,
            "parity_discipline": ("双锚硬断言:FROZEN_ERA 探针 == v6 冻结产物逐格"
                                  "全等(代码零回归);现行 16 档 bar 回放 == v13 在册"
                                  " bar 臂逐格全等(engine 零回归);不等即 raise,"
                                  "报告不落地(本轮已通过)"),
            "cascade_declarations": prep["declarations"],
            "honesty_note": ("1h 语义仅作从属帧(TF≥2h 执行路径细帧 + 4h 战役 "
                             "trigger 帧),无 1h 认知战役;mic 效果对照以 arm-M − "
                             "arm-C 为主锚(续涨常量);v16 在册(300 USDT,15 战役,"
                             "无 mic)只作跨宇宙背景;sizing 差为 dev-only 机械测量;"
                             "不渲染任何转正叙述。"),
        },
        "config": {
            "config_file": "config/v17.json",
            "parent": "config/v16.json(逐键硬对账,run 前通过)",
            "tf_list": TF_LIST, "equity0": EQUITY0,
            "risk_frac": raw_cfg["risk_frac"],
            "leverage_cap": raw_cfg["leverage_cap"],
            "taker_fee": raw_cfg["taker_fee"],
            "maker_fee": raw_cfg["maker_fee"],
            "slippage_per_side": raw_cfg["slippage_per_side"],
            "funding_per_8h_per_side": raw_cfg["funding_per_8h_per_side"],
            "portfolio_cap_frac": raw_cfg["portfolio_cap_frac"],
            "dev_frac": dev_frac,
            "cost_mults": list(COST_MULTS), "judge_cost_mult": JUDGE_COST,
            "trigger_tf_map": {str(k): v for k, v in TRIGGER_TF_MAP.items()},
            "arms": {a: raw_cfg["arms"][a]["note"] for a in ARM_IDS},
            "v16_params": raw_cfg["v16_params"],
            "whitelist": [f"{i}|{'long' if d > 0 else 'short'}"
                          for i, d in sorted(whitelist)],
        },
        "contracts": {"source": spec_src,
                      "cache_path": str(CONTRACTS_PATH.relative_to(ROOT)),
                      "cache_sha256": contracts_sha},
        "cross_checks_canonical_baseline": xcheck,
        "bar_stream_parity": {"description": "mic 帧复放 bar 事件流 == "
                                             "prepare_cognition 逐字段(硬断言)",
                              "n_events_by_campaign": prep["bar_parity"]},
        "parity_frozen_era_probe": parity_probe,
        "parity_bar16_vs_v13": parity_bar16,
        "signals_by_tf": sig,
        "directive_outcome": directive_outcome,
        "v16_in_register_background": {
            "note": ("v16 在册数字(equity=300,tf×5,无 mic)仅作跨宇宙背景参照,"
                     "不参与裁决;权益/宇宙双差如实标注"),
            **v16_bg},
        "test_once_exposure": {"note": exposure_note, **test_once},
    }
    for arm_id in ARM_IDS:
        peer = "arm-C" if arm_id == "arm-M" else "arm-M"
        own = {
            "arm": arm_id,
            "mic_families": ("all" if arms_spec[arm_id]["mic_families"] is None
                             else list(arms_spec[arm_id]["mic_families"])),
            "peer_comparison_dev_1_5x": {
                "self": {g: {f"@{m}x": _cellm(grids[arm_id][g][m][1])
                             for m in COST_MULTS} for g in SEGMENTS},
                "peer": {f"dev@{m}x": _cellm(grids[peer]["dev"][m][1])
                         for m in COST_MULTS},
                "delta_dev_1_5x": directive_outcome,
            },
            "sizing_probe_300_vs_1000": {
                f"dev@{m}x": attr[arm_id][f"probe300|dev@{m}x"]["sizing"] for m in
                COST_MULTS},
            "sizing_1000": {f"dev@{m}x": sizing_row(grids[arm_id]["dev"][m][0])
                            for m in COST_MULTS},
            "explain_1d": {
                f"dev@{JUDGE_COST}x": explain_1d(
                    arm_id, "dev", attr[arm_id][f"dev@{JUDGE_COST}x"]["tf_ledger"]),
                "test@1x": explain_1d(
                    arm_id, "test", attr[arm_id]["test@1.0x"]["tf_ledger"])},
            "runs": {},
        }
        for seg in SEGMENTS:
            for mult in COST_MULTS:
                res, m = grids[arm_id][seg][mult]
                tag = f"{arm_id}|{seg}@{mult}x"
                own["runs"][tag] = {
                    "metrics": m, "counters": res.counters,
                    **{k: attr[arm_id][f"{seg}@{mult}x"][k] for k in
                       ("clause", "harvest", "family", "tf_ledger",
                        "mic_share", "advance", "giveback", "buckets",
                        "n_trades_1d")}}
        (REPORTS / f"v17_{arm_id}.json").write_text(
            json.dumps({**common, **own}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        (REPORTS / f"v17_{arm_id}.md").write_text(
            render_md(arm_id, {**common, **own}, grids, attr, v16_bg),
            encoding="utf-8")
        for seg in SEGMENTS:
            for mult in COST_MULTS:
                suffix = "" if mult == 1.0 \
                    else f"_cost{str(mult).replace('.', '_')}"
                trades_frame_v17(grids[arm_id][seg][mult][0].trades).to_csv(
                    REPORTS / f"trades_v17_{arm_id}__{seg}{suffix}.csv",
                    index=False)
        for mult in COST_MULTS:
            suffix = "" if mult == 1.0 \
                else f"_cost{str(mult).replace('.', '_')}"
            trades_frame_v17(probes300[arm_id][mult][0].trades).to_csv(
                REPORTS / f"trades_v17_probe300_{arm_id}__dev{suffix}.csv",
                index=False)
    print(f"-> reports/v17_arm-{{M,C}}.json/.md + trades CSVs "
          f"(total {time.time() - t_start:.0f}s)", flush=True)


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=2, dash="—"):
    if x is None or (isinstance(x, float) and x != x):
        return dash
    if isinstance(x, float) and x == float("inf"):
        return "inf"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def _mrow(m: dict) -> str:
    return (f"{_f(m['net_pnl'])} | {m['n_trades']} | {_f(m['winrate'], 3)} "
            f"| {_f(m['profit_factor'])} | {_f(m['expectancy_usdt'], 4)} "
            f"| {_f(m['expectancy_r'], 4)}")


def render_md(arm_id: str, payload: dict, grids: dict, attr: dict,
              v16_bg: dict) -> str:
    peer = "arm-C" if arm_id == "arm-M" else "arm-M"
    out = payload["directive_outcome"]
    lines = [
        f"# v17 指令变体回测报告 —— {arm_id}"
        f"({'mic 全族' if arm_id == 'arm-M' else '仅续涨 mic / fade 归 bar'};"
        " equity=1000,tf=[2h,4h,12h,1d])",
        "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 地位与诚实性声明",
        "",
        f"- {payload['protocol']['directive_clause']}",
        f"- 预注册:{payload['protocol']['pre_registration']};"
        f"**{payload['protocol']['test_eval_declaration']}**",
        f"- parity 纪律:{payload['protocol']['parity_discipline']}",
        "- 级联声明:"
        + (";".join(payload["protocol"]["cascade_declarations"])
           if payload["protocol"]["cascade_declarations"]
           else "映射全部精确命中,无声明。"),
        f"- {payload['protocol']['honesty_note']}",
        f"- 合约:{payload['contracts']['source']}(离线强制)。",
        "",
        "## 1. 判定总览(dev 主评 @1.5x;净/PF/n 与逐档对照)", "",
        "| 臂 | 段@成本 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT | 期望R |",
        "|---|---|---|---|---|---|---|---|"]
    for a in (arm_id, peer):
        for seg in SEGMENTS:
            for mult in COST_MULTS:
                m = grids[a][seg][mult][1]
                lines.append(f"| {a} | {seg}@{mult}x | {_mrow(m)} |")
    lines += ["",
              f"| Δ({arm_id} − {peer}) | dev@1.5x | "
              f"{out['delta_net_pnl']:+.2f} | "
              f"{grids[arm_id]['dev'][1.5][1]['n_trades']
                 - grids[peer]['dev'][1.5][1]['n_trades']:+d} | — | — "
              f"| {_f(out['delta_expectancy_usdt'], 4)} | — |",
              "",
              f"- **mic 对照主锚(dev@1.5x 期望差,全族 − 仅续涨):"
              f"{_f(out['delta_expectancy_usdt'], 4)} USDT ⇒ "
              f"{out['outcome']}**",
              "",
              "### 1a. v16 在册背景(跨宇宙参照,不参与裁决)", "",
              "| 参照 | 权益 | TF 数 | mic | 净盈亏 | 笔数 | PF | 期望USDT |",
              "|---|---|---|---|---|---|---|---|"]
    for k, c in v16_bg.items():
        lines.append(f"| v16 在册 {k} | 300 | 5 | 无 | {_f(c['net_pnl'])} "
                     f"| {c['n_trades']} | {_f(c['profit_factor'])} "
                     f"| {_f(c['expectancy_usdt'], 4)} |")
    for a in (arm_id, peer):
        m = grids[a]["dev"][1.5][1]
        lines.append(f"| v17 {a} | 1000 | 4 | {'全族' if a == 'arm-M' else '续涨'} "
                     f"| {_f(m['net_pnl'])} | {m['n_trades']} "
                     f"| {_f(m['profit_factor'])} "
                     f"| {_f(m['expectancy_usdt'], 4)} |")

    lines += ["", "## 2. 逐 TF 净账(dev@1.5x;信号/闸/成交/拒绝全同一格)", "",
              "| 臂 | TF | 信号bar | 信号mic | 闸评估 | q1拒 | q2拒 | f1拒 | "
              "笔数 | 净额 | min_size | 容量跳 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in (arm_id, peer):
        led = attr[a][f"dev@{JUDGE_COST}x"]["tf_ledger"]
        for tf in TF_LIST:
            c = led[tf]
            lines.append(
                f"| {a} | {tf} | {c['signals_bar']} | {c['signals_mic']} "
                f"| {c['gate_evaluated']} | {c['gate_reject_q1_pos']} "
                f"| {c['gate_reject_q2_room']} | {c['gate_reject_f1']} "
                f"| {c['n_trades']} | {_f(c['net_pnl'])} "
                f"| {c['min_size_skips']} | {c['portfolio_cap_skips']} |")
    lines += ["", "### 2a. 1d 交易出现数(及机读归因)", ""]
    for a in (arm_id, peer):
        lines.append(f"- 1d n(dev@1.5x)="
                     f"{attr[a][f'dev@{JUDGE_COST}x']['n_trades_1d']}"
                     f" / 1d n(test@1x)={attr[a]['test@1.0x']['n_trades_1d']}:"
                     f" {payload['explain_1d'][f'dev@{JUDGE_COST}x'] if a == arm_id else '(见对侧报告)'}")
    lines += [f"- 本臂 1d(test 口径):{payload['explain_1d']['test@1x']}", ""]

    lines += ["## 3. sizing 差:1000 刀 vs 300 刀(dev 三格,min_size 拒绝率)", "",
              "| 臂 | 格 | 权益 | open_intents | MIN_SIZE | 拒绝率 | 成交 |",
              "|---|---|---|---|---|---|---|"]
    for mult in COST_MULTS:
        s1000 = payload["sizing_1000"][f"dev@{mult}x"]
        s300 = payload["sizing_probe_300_vs_1000"][f"dev@{mult}x"]
        lines.append(
            f"| {arm_id} | dev@{mult}x | 1000 | {s1000['open_intents']} "
            f"| {s1000['min_size_skips']} | {_f(s1000['min_size_rate'], 4)} "
            f"| {s1000['fills']} |")
        lines.append(
            f"| {arm_id} | dev@{mult}x | 300(探针) | {s300['open_intents']} "
            f"| {s300['min_size_skips']} | {_f(s300['min_size_rate'], 4)} "
            f"| {s300['fills']} |")
    peer_min_size = " / ".join(
        f"dev@{m}x: {sizing_row(grids[peer]['dev'][m][0])['min_size_skips']}"
        f"/{sizing_row(grids[peer]['dev'][m][0])['open_intents']}"
        for m in COST_MULTS)
    lines += ["",
              f"> 探针 = 同臂同语义仅权益 300 重放(dev-only,不构成 test 曝光);"
              f"拒绝率 = MIN_SIZE SKIP / open_intents(过容量闸后的订单);对侧臂 "
              f"{peer}(1000 刀)min_size/intents = {peer_min_size};"
              "v16 在册(300,15 战役)背景见 reports/v16.json counters。",
              ""]

    lines += ["## 4. mic 效果对照(入口占比 + 提前量;dev@1.5x)", "",
              "| 臂 | 家族 | mic 成交 | bar 成交 |",
              "|---|---|---|---|"]
    for a in (arm_id, peer):
        sh = attr[a][f"dev@{JUDGE_COST}x"]["mic_share"]
        for fam in ("fade", "continuation", "other"):
            if fam in sh:
                lines.append(f"| {a} | {fam} | {sh[fam]['mic']} "
                             f"| {sh[fam]['bar']} |")
    adv = attr[arm_id][f"dev@{JUDGE_COST}x"]["advance"]["overall_market"]
    lines += ["",
              f"- 本臂 mic 市价成交提前量(相对认知收盘,分钟):n="
              f"{adv['n']} 均 {_f(adv['mean_min'], 1)} p50 "
              f"{_f(adv['p50_min'], 0)} p75 {_f(adv['p75_min'], 0)} "
              f"min {_f(adv['min_min'], 0)} max {_f(adv['max_min'], 0)};",
              ""]

    cl126 = attr[arm_id][f"dev@{JUDGE_COST}x"]["clause"]
    lines += ["## 5. 条款生效统计(本臂 dev@1.5x)", "",
              f"- 三闸:评估 {cl126['v16_gate_evaluated']},拒 q1="
              f"{cl126['v16_gate_reject_q1_pos']} q2="
              f"{cl126['v16_gate_reject_q2_room']} f1={cl126['v16_gate_reject_f1']};"
              f"档 A {cl126['v16_tier_a']} / B {cl126['v16_tier_b']};"
              f"fade 无目标 {cl126['v16_fade_no_target']}",
              f"- 执行差分:stop 埋 {cl126['stop_entry_armed']} 触发 "
              f"{cl126['stop_entry_triggers']} 成交 {cl126['stop_entry_fills']}"
              f"(率 {_f(cl126['stopex_fill_rate'], 3)});maker 埋 "
              f"{cl126['maker_entry_armed']} 成交 {cl126['maker_entry_fills']}"
              f"(率 {_f(cl126['maker_fill_rate'], 3)});容量跳 "
              f"{cl126['portfolio_cap_skips']};mic_family_filtered="
              f"{payload['runs'][f'{arm_id}|dev@{JUDGE_COST}x']['counters'].get('mic_family_filtered', 0)}",
              "",
              "### 5a. 收割桶 × 分家族桶(本臂 dev@1.5x)", "",
              "| 桶 | 笔数 | 净额 | 胜率 | PF |", "|---|---|---|---|---|"]
    for src, label in ((attr[arm_id][f"dev@{JUDGE_COST}x"]["harvest"], "收割"),
                       (attr[arm_id][f"dev@{JUDGE_COST}x"]["family"], "家族")):
        for b, c in src.items():
            lines.append(f"| {label}:{b} | {c['n_trades']} "
                         f"| {_f(c['net_pnl'])} | {_f(c['winrate'], 3)} "
                         f"| {_f(c['profit_factor'])} |")
    lines.append("")

    to = payload["test_once_exposure"]
    up = to[arm_id]["upgrade_line"]
    lines += ["## 6. test 一次(第 20 次曝光;如实写 dev/test 对照)", "",
              f"- {to['note']}", "",
              "| 臂 | 格 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT | 期望R |",
              "|---|---|---|---|---|---|---|---|"]
    for a in (arm_id, peer):
        cell = to[a]
        for kk in ("dev_1x", "dev_1_5x", "dev_2x", "test_1x", "test_1_5x",
                   "test_2x"):
            c = cell[kk]
            lines.append(f"| {a} | {kk.replace('_', '@', 1)} "
                         f"| {_mrow(c)} |")
    lo, hi = to[arm_id]["test_net_pnl_mean_ci95_1x"]
    lines += ["",
              f"- 本臂 test@1x 逐笔净盈亏均值 95% CI(自然日 block bootstrap,"
              f"2000 次,固定种子):{_f(lo, 4)} ~ {_f(hi, 4)} USDT(如实报告)",
              "- 既往升级线逐条照报(本臂):",
              f"  - test 笔数:{up['n_trades_test_1x']} ⇒ {up['sample_size']}",
              f"  - 1.5x 净期望:{_f(up['expectancy_usdt_test_1_5x'], 4)} "
              f"USDT ⇒ {'>0 ✔' if up['expectancy_positive_1_5x'] else '≤0 ✘'}",
              f"  - 2x PF:{_f(up['profit_factor_test_2_0x'])} "
              f"⇒ {'>1 ✔' if up['pf_gt_1_2_0x'] else '≤1 ✘'}",
              f"  - **判定(本臂):{up['verdict']}**",
              "",
              "> 不渲染任何转正叙述;test<60 ⇒ INSUFFICIENT_DATA;升级线不过线即 "
              "Research Only 维持。", ""]

    lines += ["## 7. 事件 × 方向 × TF 归因(dev@1.5x)", ""]
    for a in (arm_id, peer):
        bt = attr[a][f"dev@{JUDGE_COST}x"]["buckets"]
        lines += [f"### {a}", "",
                  "| 机制 | TF | 方向 | 笔数 | 胜率 | 平均R | 净额 |",
                  "|---|---|---|---|---|---|---|"]
        any_rows = False
        for mech in ("BREAKOUT_ACCEPTED", "BREAKOUT_FAILED", "LEVEL_REJECTED", "TREND_RESUMED", "RANGE_ROTATED", "STRUCTURE_BROKEN", "FLIP_RETESTED", "EDGE_RECLAIMED"):
            for tf in TF_LIST:
                for side in ("short", "long"):
                    cell = bt.get(mech, {}).get(tf, {}).get(side)
                    if cell is None:
                        continue
                    any_rows = True
                    lines.append(f"| {mech} | {tf} | {side} | {cell['n_trades']} "
                                 f"| {_f(cell['winrate'], 3)} "
                                 f"| {_f(cell['avg_r'], 3)} "
                                 f"| {_f(cell['net_pnl'])} |")
        if not any_rows:
            lines.append("| — | — | — | 0 | — | — | — |")
        lines.append("")

    lines += ["## 8. 已知近似与如实声明", "",
              "① mic 层语义全沿 v10/v13 冻结:结构锁定最近已收认知 bar、数量闸 "
              "N×scale、12h→2h/1d→4h 声明档、次一 trigger 开盘成交;② v17 提交"
              "钩:mic 决策过与认知收盘同一 _arm_route(C2 maker / C3 stop-entry),"
              "阈值=frozen 语境 ATR;stop-entry 超时窗锚 = 本小时桶前一小时 + "
              "12×TF(与认知路径同一公式,锚点略早于决策收盘,方向保守——过期"
              "偏严,如实声明);③ arm-C 的 fade 事件仍在认知收盘按 bar 语义路由"
              "(与 v13 bar 流一致);续涨的 bar 化身为同现象备份(槽位先到先得,"
              "v13 既定口径);④ sizing 探针只落 dev:dev=probe 的权益是唯一变量,"
              "组合容量 3% 对 1000 USDT 为 30 USDT 预算(同比例,绝对预算随权益"
              "放大,如实声明);⑤ 1h 下架机制 = tf_list 不含 1h(认知战役不建);"
              "1h 帧仅执行细帧与 4h trigger 帧(声明见 §0);⑥ 组合 3% 容量决策"
              "账未乘档(保守,同 v14/v16 声明);⑦ mic 市价提前量以 OPEN 成交"
              "度量;挂单成交含窗口等待(可晚于认知收盘),分栏列示;⑧ v16 在册"
              "背景 = equity 300 × 15 战役 × 无 mic 宇宙,数字只作跨宇宙参照;"
              "⑨ test 曝光纪律见 §6;sizing 探针不构成 test 评估。",
              "",
              f"机器可读全量:`reports/v17_{arm_id}.json`;逐笔:"
              f"`reports/trades_v17_{arm_id}__*" ".csv`(增列 mic/cog_index/"
              f"advance_minutes);sizing 探针:`reports/trades_v17_probe300_"
              f"{arm_id}__dev*.csv`。对侧臂:`reports/v17_{peer}.json`。",
              ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
