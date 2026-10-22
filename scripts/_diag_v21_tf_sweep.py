"""v21 触发映射 / 触发确认方式对比诊断(T0-T3 × F0-F3 的轻量复算)。

对齐 .codeartsdoer/specs/tf_trigger_exp/spec.md 的因子定义:
  因子 T(触发帧;12h->2h、1d->4h 固定照抄 M3c):
    T0 = 2h 同周期 + 4h->2h(现行 config/v21.json)
    T1 = 2h->1h + 4h->2h
    T2 = 2h 同周期 + 4h 不下沉
    T3 = 2h->1h + 4h 不下沉
  因子 F(触发确认方式):
    F0 = htf_atr 绑定(现行 [§A 阈值] 语义;min_bar_impulse_atr 按 HTF ATR)
    F1 = 固定阈值对照(以 min_bar_impulse_atr 松弛/收紧近似)
    F2 = 子周期 ChoCH 破位确认(trigger bar 收盘破坏前 N 根极值 -> 事件保留)
    F3 = intra-bar 时间进度门控(事件落在认知 bar 后 p% 时间区间才放行)

用法:
    python scripts/_diag_v21_tf_sweep.py --stage map
    python scripts/_diag_v21_tf_sweep.py --stage imp
    python scripts/_diag_v21_tf_sweep.py --stage filt
    python scripts/_diag_v21_tf_sweep.py --stage small
    python scripts/_diag_v21_tf_sweep.py --stage detail
    python scripts/_diag_v21_tf_sweep.py --stage why
    python scripts/_diag_v21_tf_sweep.py --stage all

地位: 只读诊断(不改 config/、不改 src/);不落盘任何产物。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace as dc_replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import _diag_v21_backtest as diag                                  # noqa: E402
import run_backtest_v6 as rb                                       # noqa: E402
from market_operator import aggregate as agg                       # noqa: E402
from market_operator.campaign import (                             # noqa: E402
    ArmConfig,
    build_campaign_frame,
    run_campaigns,
)
from market_operator.structure import Params                       # noqa: E402
from market_operator.routing import RouterParams                   # noqa: E402
from market_operator.trigger import prepare_mic_cognition          # noqa: E402

CONFIG = ROOT / "config" / "v21.json"
HTF = ("2h", "4h", "12h", "1d")

# 触发帧秒数(None = 同周期收盘确认)
MAPS = {
    "T0 现行 2h同/4h->2h": {"2h": None, "4h": 7200, "12h": 7200, "1d": 14400},
    "T1 2h->1h / 4h->2h": {"2h": 3600, "4h": 7200, "12h": 7200, "1d": 14400},
    "T2 2h同 / 4h不下沉": {"2h": None, "4h": None, "12h": 7200, "1d": 14400},
    "T3 2h->1h / 4h不下沉": {"2h": 3600, "4h": None, "12h": 7200, "1d": 14400},
}


# ---------------------------------------------------------------------------
# 触发事件后处理(F2 ChoCH / F3 进度门控;纯脚本层,不改 src/)
# ---------------------------------------------------------------------------


def filter_mic(mic, mode: str, param: float = 0.0):
    """对 MicCognition.events_by 做后处理(views/atrs/bars 原样保留)。

    mode:
      "choch_prev" : trigger bar 收盘须破坏前一根 trigger bar 极值(事件方向)
      "choch_ext3" : trigger bar 收盘须破坏前三根 trigger bar 极值(事件方向)
      "progress"   : 事件须落在其认知 bar 时间区间的前 param 之后(param 为
                     已过比例;0.5 = 后半段才放行)
    """
    new_ev_by: dict = {}
    first_of: dict = {}
    for a, i in enumerate(mic.cog_index_of):
        first_of.setdefault(int(i), a)

    for a, evs in mic.events_by.items():
        kept = []
        c = mic.bars[a][4]
        for ev in evs:
            if mode == "choch_prev":
                if a < 1:
                    continue
                ph, pl = mic.bars[a - 1][2], mic.bars[a - 1][3]
                ok = (c > ph) if ev.direction > 0 else (c < pl)
                if not ok:
                    continue
            elif mode == "choch_ext3":
                if a < 3:
                    continue
                hs = [mic.bars[k][2] for k in range(a - 3, a)]
                ls = [mic.bars[k][3] for k in range(a - 3, a)]
                ok = (c > max(hs)) if ev.direction > 0 else (c < min(ls))
                if not ok:
                    continue
            elif mode == "progress":
                scale = int(mic.scale)
                pos = (a - first_of[int(mic.cog_index_of[a])]) / scale
                if pos < float(param):
                    continue
            kept.append(ev)
        if kept:
            new_ev_by[a] = kept
    return dc_replace(mic, events_by=new_ev_by)


# ---------------------------------------------------------------------------
# 数据/参数装配
# ---------------------------------------------------------------------------


class Ctx:
    def __init__(self) -> None:
        self.cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        syms = [s for s in self.cfg["symbols"]
                if (ROOT / "data" / "raw" / f"{s}_1h.csv").exists()]
        missing = [s for s in self.cfg["symbols"] if s not in syms]
        if missing:
            print(f"[warn] 无 1h 原始数据, 本轮剔除: {missing} "
                  f"(v21 四品种口径需补数据)", flush=True)
        self.syms = syms
        self.contracts, _src = rb.load_contracts_cache(syms)
        base = Params.from_json(CONFIG)
        self.params = Params(**{**vars(base), "use_balance_detector": True,
                                "balance_quality_min": 0.35})
        t0 = time.time()
        (self.cog_by, self.df_cog_by, self.df_exec_by,
         self.df_trig_by) = diag.load_all_data(syms, self.params, tf_list=HTF)
        self.mic_cache: dict = {}
        self.frames_cache: dict = {}
        print(f"[prep] 数据就绪 {syms} ({time.time() - t0:.1f}s)", flush=True)

        cfg = self.cfg
        wl = frozenset((str(e), int(d)) for e, d in cfg["whitelist"])
        self.base_rp = RouterParams(
            k_dq=float(cfg["k_dq"]), k_imp=float(cfg["k_imp"]),
            k_is=float(cfg["k_is"]),
            turtle_stop_atr=float(cfg["turtle_stop_atr"]),
            k_bm=float(cfg["k_bm"]), whitelist=wl,
            **cfg.get("router_params", {}))
        self.rp_by_tf = {
            tf_k: dc_replace(self.base_rp, **ovr)
            for tf_k, ovr in cfg["router_params_by_tf"].items()}
        self.arm = ArmConfig(**cfg["arm_config"])
        self.sc = cfg["small_account_compounding"]

    # -- frames ------------------------------------------------------------
    def mic_for(self, sym, tf, trig_sec, filt):
        obs_sec = agg.TF_HOURS[tf] * 3600
        key = (sym, tf, trig_sec)
        if key not in self.mic_cache:
            self.mic_cache[key] = prepare_mic_cognition(
                self.df_cog_by[(sym, tf)], self.df_trig_by[sym][trig_sec],
                self.params, sym, obs_tf_seconds=obs_sec,
                trigger_tf_seconds=trig_sec, enable_e7=True)
        mic = self.mic_cache[key]
        if filt is not None:
            mode, param = filt
            mic = filter_mic(mic, mode, param)
        return mic

    def frames(self, trig_map: dict, filters: dict | None = None) -> dict:
        filters = filters or {}
        fkey = (tuple(sorted(trig_map.items(), key=lambda kv: kv[0])),
                tuple(sorted(filters.items())))
        if fkey in self.frames_cache:
            return self.frames_cache[fkey]
        out = {}
        for sym in self.syms:
            for tf in HTF:
                cog = self.cog_by[(sym, tf)]
                df_exec = self.df_exec_by[(sym, tf)]
                trig_sec = trig_map.get(tf)
                mic = None
                if trig_sec is not None:
                    mic = self.mic_for(sym, tf, trig_sec, filters.get(tf))
                f = build_campaign_frame(cog, df_exec, tf, mic_cog=mic)
                out[f.key] = f
        self.frames_cache[fkey] = out
        return out

    # -- run ---------------------------------------------------------------
    def rp(self, overrides: dict | None = None) -> dict:
        rp = dict(self.rp_by_tf)
        for tf_k, ovr in (overrides or {}).items():
            rp[tf_k] = dc_replace(rp[tf_k], **ovr)
        return rp

    def run(self, frames: dict, *, segment="full", cm=1.0, small=False,
            rp_by_tf=None):
        cfg, sc = self.cfg, self.sc
        cap = (float(sc["portfolio_cap_frac"]) if small
               else float(cfg["portfolio_cap_frac"]))
        tfcap = sc["tf_cap_by_tf"] if small else cfg["tf_cap_by_tf"]
        return run_campaigns(
            frames, segment=segment, dev_frac=float(cfg["dev_frac"]),
            equity0=float(sc["equity0"]) if small else 300.0,
            risk_frac=float(cfg["risk_frac"]),
            leverage_cap=float(sc["leverage_cap"]),
            taker_fee=float(cfg["taker_fee"]),
            maker_fee=float(cfg["maker_fee"]),
            slippage_per_side=float(cfg["slippage_per_side"]),
            funding_per_8h=float(cfg["funding_per_8h_per_side"]),
            cost_multiplier=cm, contracts=self.contracts,
            params=self.params, n_fill=int(cfg["n_fill"]),
            portfolio_cap_frac=cap, tf_cap_by_tf=tfcap,
            dynamic_risk_tiers=(sc["dynamic_risk_tiers"] if small else None),
            arm=self.arm, router_params=self.base_rp, enable_mic=True,
            router_params_by_tf=(rp_by_tf if rp_by_tf is not None
                                 else self.rp_by_tf))



# ---------------------------------------------------------------------------
# 指标与打印
# ---------------------------------------------------------------------------


def metrics(res, equity0: float) -> dict:
    c = [t for t in res.trades if t.get("closed") and t.get("in_segment_open")]
    gross = sum(t["gross_pnl"] for t in c)
    net = sum(t["net_pnl"] for t in c)
    cost = sum(t["fees"] + t["funding"] + t["slippage_cost"] for t in c)
    grs = [t["gross_pnl"] / t["risk_usdt"] for t in c if t["risk_usdt"]]
    wins = [t for t in c if t["gross_pnl"] > 0]
    gw = sum(t["gross_pnl"] for t in wins)
    gl = -sum(t["gross_pnl"] for t in c if t["gross_pnl"] <= 0)
    peak, dd = -1e18, 0.0
    for row in res.equity_rows:
        e = float(row[1])
        peak = max(peak, e)
        if peak > 0:
            dd = max(dd, (peak - e) / peak)
    return {
        "n": len(c), "gross": gross, "cost": cost, "net": net,
        "eq": equity0 + net,
        "gr": (sum(grs) / len(grs)) if grs else 0.0,
        "wr": (len(wins) / len(c)) if c else 0.0,
        "pf": (gw / gl) if gl > 0 else float("inf"),
        "dd": dd * 100.0,
        "trades": c,
    }


HDR = (f"{'arm':42s} {'seg':4s} {'x':>4s} | {'N':>4s} {'gross':>7s} "
       f"{'cost':>6s} {'net':>7s} {'eq':>7s} | {'gR':>6s} {'wr':>5s} "
       f"{'PF':>5s} {'DD%':>5s}")


def report(label: str, m: dict, segment: str, cm: float,
           by_tf: bool = False) -> None:
    print(f"{label:42s} {segment:4s} {cm:4.1f} | {m['n']:4d} "
          f"{m['gross']:+7.1f} {m['cost']:6.1f} {m['net']:+7.1f} "
          f"{m['eq']:7.1f} | {m['gr']:+6.3f} {m['wr']:5.3f} "
          f"{m['pf']:5.2f} {m['dd']:5.2f}", flush=True)
    if not by_tf:
        return
    for tf_k in HTF:
        sub = [t for t in m["trades"] if t.get("tf") == tf_k]
        if not sub:
            continue
        s_net = sum(t["net_pnl"] for t in sub)
        s_gross = sum(t["gross_pnl"] for t in sub)
        s_gr = [t["gross_pnl"] / t["risk_usdt"] for t in sub if t["risk_usdt"]]
        s_w = [t for t in sub if t["gross_pnl"] > 0]
        print(f"    +-- {tf_k:3s}: N={len(sub):3d} gross={s_gross:+7.1f} "
              f"net={s_net:+7.1f} gR={sum(s_gr) / len(s_gr):+.3f} "
              f"wr={len(s_w) / len(sub):.3f}", flush=True)


def block(ctx: Ctx, label: str, frames: dict, *, rp_by_tf=None,
          by_tf_full: bool = True) -> None:
    print(f"\n### {label}")
    for seg in ("dev", "test", "full"):
        for cm in (1.0, 1.5, 2.0):
            if seg != "full" and cm > 1.0:
                continue
            m = metrics(ctx.run(frames, segment=seg, cm=cm,
                                rp_by_tf=rp_by_tf), 300.0)
            report(label, m, seg, cm,
                   by_tf=(by_tf_full and seg == "full" and cm == 1.0))



# ---------------------------------------------------------------------------
# 阶段
# ---------------------------------------------------------------------------


def stage_map(ctx: Ctx) -> None:
    print("\n" + "=" * 78)
    print("阶段 1:因子 T(触发帧映射)× 现行 F0(htf_atr 绑定)")
    print("=" * 78)
    for label, mp in MAPS.items():
        block(ctx, label, ctx.frames(mp))


def stage_imp(ctx: Ctx) -> None:
    print("\n" + "=" * 78)
    print("阶段 2:min_bar_impulse_atr 扫描(htf_atr 绑定强度)")
    print("=" * 78)
    for mp_label, mp, tf_key in (
        ("T0 现行 2h同/4h->2h", MAPS["T0 现行 2h同/4h->2h"], "4h"),
        ("T1 2h->1h / 4h->2h", MAPS["T1 2h->1h / 4h->2h"], "2h"),
    ):
        frames = ctx.frames(mp)
        v21_imp = float(ctx.cfg["router_params_by_tf"][tf_key]
                        ["min_bar_impulse_atr"])
        for imp in (0.6, 0.8, 1.0, v21_imp, 1.5):
            rp = ctx.rp({tf_key: {"min_bar_impulse_atr": imp}})
            label = f"{mp_label} {tf_key}.imp={imp:g}"
            block(ctx, label, frames, rp_by_tf=rp, by_tf_full=False)


def stage_filt(ctx: Ctx) -> None:
    print("\n" + "=" * 78)
    print("阶段 3:因子 F(确认方式)—— F2 子周期 ChoCH / F3 进度门控")
    print("=" * 78)
    t0 = MAPS["T0 现行 2h同/4h->2h"]
    cases = (
        ("F0 基线(htf_atr, 无门控)", t0, None),
        ("F2 ChoCH(前一根) 4h战役", t0, {"4h": ("choch_prev", 0.0)}),
        ("F2 ChoCH(前三根) 4h战役", t0, {"4h": ("choch_ext3", 0.0)}),
        ("F3 进度门控 50% 4h战役", t0, {"4h": ("progress", 0.5)}),
        ("F3 进度门控 75% 4h战役", t0, {"4h": ("progress", 0.75)}),
        ("F2 ChoCH(前一根) 2h战役", t0, {"2h": ("choch_prev", 0.0)}),
        ("F2 ChoCH(前一根) 全战役", t0, {tf: ("choch_prev", 0.0)
                                          for tf in HTF}),
    )
    for label, mp, filters in cases:
        block(ctx, label, ctx.frames(mp, filters), by_tf_full=False)


def stage_small(ctx: Ctx) -> None:
    print("\n" + "=" * 78)
    print("阶段 4:104U / 20x 阶梯复利(现行 T0 与 T1/T2 对照)")
    print("=" * 78)
    for label, mp in (
        ("T0 现行 2h同/4h->2h", MAPS["T0 现行 2h同/4h->2h"]),
        ("T1 2h->1h / 4h->2h", MAPS["T1 2h->1h / 4h->2h"]),
        ("T2 2h同 / 4h不下沉", MAPS["T2 2h同 / 4h不下沉"]),
    ):
        frames = ctx.frames(mp)
        for seg in ("dev", "test", "full"):
            for cm in (1.0, 1.5):
                if seg != "full" and cm > 1.0:
                    continue
                m = metrics(ctx.run(frames, segment=seg, cm=cm, small=True),
                            float(ctx.sc["equity0"]))
                report(label, m, seg, cm)


def _ev_counts(mb, mf) -> tuple:
    """(事件数, 保留数, 事件 bar 数, 保留 bar 数, 被删 (ts, direction) 列表)。"""
    n_ev = n_kept = n_bar = n_bar_kept = 0
    dropped = []
    for a, evs in mb.events_by.items():
        n_ev += len(evs)
        n_bar += 1
        kb = mf.events_by.get(a, ())
        n_kept += len(kb)
        if kb:
            n_bar_kept += 1
        ids = {(e.id, e.direction) for e in kb}
        for ev in evs:
            if (ev.id, ev.direction) not in ids:
                dropped.append((int(mb.bars[a][0]), int(ev.direction)))
    return n_ev, n_kept, n_bar, n_bar_kept, dropped


def _trade_key(t) -> tuple:
    return (t["symbol"], t.get("tf"), int(t["open_decision_ts"]))


def stage_why(ctx: Ctx) -> None:
    """4h->2h × ChoCH 归因:事件通过率 + 逐笔集合差分(删除/新增/共有时点漂移)。"""
    print("\n" + "=" * 78)
    print("阶段 6:ChoCH / 进度门控归因(通过率 / 删除 / 新增 / 同点漂移)")
    print("=" * 78)
    t0 = MAPS["T0 现行 2h同/4h->2h"]
    base = metrics(ctx.run(ctx.frames(t0), segment="full", cm=1.0), 300.0)
    base_tf = {tf: sum(t["net_pnl"] for t in base["trades"] if t.get("tf") == tf)
               for tf in HTF}
    print(f"基线 full@1.0x: N={base['n']} net={base['net']:+.1f} | "
          + " ".join(f"{tf}={base_tf[tf]:+.1f}" for tf in HTF), flush=True)

    for mode, param, tfs in (("choch_prev", 0.0, ("4h",)),
                             ("choch_prev", 0.0, ("4h", "12h", "1d")),
                             ("choch_ext3", 0.0, ("4h",)),
                             ("progress", 0.5, ("4h",))):
        frames = ctx.frames(t0, {tf: (mode, param) for tf in tfs})
        m = metrics(ctx.run(frames, segment="full", cm=1.0), 300.0)
        tf_net = {tf: sum(t["net_pnl"] for t in m["trades"] if t.get("tf") == tf)
                  for tf in HTF}
        print(f"\n### {mode} (param={param:g}) 作用域={'+'.join(tfs)}  "
              f"Δnet={m['net'] - base['net']:+.1f}  N={m['n']} | "
              + " ".join(f"{tf}={tf_net[tf] - base_tf[tf]:+.1f}" for tf in HTF),
              flush=True)
        for tf in tfs:
            trig = t0[tf]
            n_ev = n_kept = 0
            for sym in ctx.syms:
                e0, e1, _b0, _b1, _d = _ev_counts(
                    ctx.mic_for(sym, tf, trig, None),
                    ctx.mic_for(sym, tf, trig, (mode, param)))
                n_ev += e0
                n_kept += e1
            bmap: dict = {}
            for t in base["trades"]:
                if t.get("tf") == tf:
                    bmap.setdefault(_trade_key(t), []).append(t)
            fmap: dict = {}
            for t in m["trades"]:
                if t.get("tf") == tf:
                    fmap.setdefault(_trade_key(t), []).append(t)
            rem, add, d_common, n_com = [], [], 0.0, 0
            for k in bmap.keys() | fmap.keys():
                bl, fl = bmap.get(k, []), fmap.get(k, [])
                n = min(len(bl), len(fl))
                for j in range(n):
                    d_common += fl[j]["net_pnl"] - bl[j]["net_pnl"]
                n_com += n
                rem.extend(bl[n:])
                add.extend(fl[n:])
            s_rem = sum(t["net_pnl"] for t in rem)
            s_add = sum(t["net_pnl"] for t in add)
            delta = tf_net[tf] - base_tf[tf]
            print(f"  {tf:3s}: 事件通过 {n_kept:5d}/{n_ev:5d} "
                  f"({100.0 * n_kept / n_ev:5.1f}%) | 删 {len(rem):3d} 笔 "
                  f"{s_rem:+7.1f}(均 {s_rem / len(rem) if rem else 0:+5.2f}) | "
                  f"增 {len(add):3d} 笔 {s_add:+7.1f} | "
                  f"同点 {n_com:3d} 笔漂移 {d_common:+6.1f} | "
                  f"Δ={delta:+7.1f} 校验={-s_rem + s_add + d_common:+7.1f}",
                  flush=True)
        print("  (Δ ≈ -删除净额 + 新增净额 + 同点漂移;同点漂移=同一决策时刻因"
              "仓位/权益变化导致的成交差异)", flush=True)



def stage_detail(ctx: Ctx) -> None:
    """集中度检验:imp=0.8 / F2 ChoCH 的优势是否由个别大单造成。"""
    print("\n" + "=" * 78)
    print("阶段 5:集中度与稳健性明细(top-1/top-3 剔除后净额)")
    print("=" * 78)
    t0 = MAPS["T0 现行 2h同/4h->2h"]
    cases = (
        ("T0 imp=1.2 (v21)", t0, {"4h": {"min_bar_impulse_atr": 1.2}}, None),
        ("T0 imp=1.0", t0, {"4h": {"min_bar_impulse_atr": 1.0}}, None),
        ("T0 imp=0.8", t0, {"4h": {"min_bar_impulse_atr": 0.8}}, None),
        ("T0 imp=0.6", t0, {"4h": {"min_bar_impulse_atr": 0.6}}, None),
        ("T0 + F2 ChoCH(前一根)全战役", t0, None,
         {tf: ("choch_prev", 0.0) for tf in HTF}),
    )
    for label, mp, ovr, filt in cases:
        frames = ctx.frames(mp, filt)
        print(f"\n### {label}")
        for cm in (1.0, 1.5):
            m = metrics(ctx.run(frames, segment="full", cm=cm,
                                rp_by_tf=ctx.rp(ovr)), 300.0)
            nets = sorted((t["net_pnl"] for t in m["trades"]), reverse=True)
            top1 = sum(nets[:1])
            top3 = sum(nets[:3])
            report(label, m, "full", cm, by_tf=(cm == 1.0))
            print(f"    top1={top1:+7.1f} top3={top3:+7.1f} "
                  f"net-top1={m['net'] - top1:+7.1f} "
                  f"net-top3={m['net'] - top3:+7.1f} "
                  f"max_loss={min(nets):+7.1f}", flush=True)


STAGES = {"map": stage_map, "imp": stage_imp, "filt": stage_filt,
          "small": stage_small, "detail": stage_detail, "why": stage_why}



def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=["all", *STAGES.keys()])
    args = ap.parse_args()
    print(HDR)
    ctx = Ctx()
    names = list(STAGES) if args.stage == "all" else [args.stage]
    for name in names:
        STAGES[name](ctx)


if __name__ == "__main__":
    main()

