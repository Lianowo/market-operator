"""v21.5:平衡箱原语(ACCEPT/REJECT) × 跨周期阶梯触发(Mode 1 vs Mode 2 vs Mode 3) + 2h 谨慎克制对照诊断。"""
import json
import sys
import time
from dataclasses import replace as dc_replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_backtest_v6 as rb                                  # noqa: E402
from market_operator import aggregate as agg                  # noqa: E402
from market_operator import data as mo_data                   # noqa: E402
from market_operator.backtest_v6 import prepare_cognition     # noqa: E402
from market_operator.campaign import (                        # noqa: E402
    build_campaign_frame,
    run_campaigns,
)
from market_operator.strategy_v6 import V6Params              # noqa: E402
from market_operator.structure import Params                  # noqa: E402
from market_operator.trigger import prepare_mic_cognition     # noqa: E402

CONFIG = ROOT / "config" / "canonical.json"


def load_all_data(symbols, params, tf_list=("2h", "4h", "12h", "1d")):
    """预加载认知帧(BalanceDetector 高置信度平衡箱)与各细周期触发帧(30m/1h/2h/4h)。"""
    cog_by, df_cog_by, df_exec_by, df_trig_by = {}, {}, {}, {}
    for sym in symbols:
        t0 = time.time()
        df_1h = mo_data.load(sym)
        df_30m = mo_data.load(sym, "30m")
        df_2h = agg.load_tf(sym, "2h")
        df_4h = agg.load_tf(sym, "4h")
        df_12h = agg.load_tf(sym, "12h")
        df_1d = agg.load_tf(sym, "1d")
        df_trig_by[sym] = {1800: df_30m, 3600: df_1h, 7200: df_2h, 14400: df_4h}
        df_map = {"2h": df_2h, "4h": df_4h, "12h": df_12h, "1d": df_1d}
        for tf in tf_list:
            df_cog = df_map[tf]
            cog_by[(sym, tf)] = prepare_cognition(df_cog, params, sym, enable_e7=True)
            df_cog_by[(sym, tf)] = df_cog
            df_exec_by[(sym, tf)] = df_1h
        print(f"[prep] {sym} 认知+细帧就绪 ({time.time() - t0:.1f}s)", flush=True)
    return cog_by, df_cog_by, df_exec_by, df_trig_by


def build_frames_for_map(symbols, tf_list, trig_map, params, cog_by, df_cog_by,
                         df_exec_by, df_trig_by, mic_cache):
    frames = {}
    for sym in symbols:
        for tf in tf_list:
            cog = cog_by[(sym, tf)]
            df_exec = df_exec_by[(sym, tf)]
            trig_sec = trig_map.get(tf)
            mic = None
            if trig_sec is not None:
                ckey = (sym, tf, trig_sec)
                if ckey not in mic_cache:
                    obs_sec = agg.TF_HOURS[tf] * 3600
                    mic_cache[ckey] = prepare_mic_cognition(
                        df_cog_by[(sym, tf)], df_trig_by[sym][trig_sec], params, sym,
                        obs_tf_seconds=obs_sec, trigger_tf_seconds=trig_sec,
                        enable_e7=True)
                mic = mic_cache[ckey]
            f = build_campaign_frame(cog, df_exec, tf, mic_cog=mic)
            frames[f.key] = f
    return frames


def eval_run(label, frames, raw, contracts, params, eids=("BREAKOUT_ACCEPTED", "LEVEL_REJECTED"),
             enable_mic=False, rp_override=None, rp_by_tf_override=None,
             segment="full", cost_mult=1.0, show_by_tf=False):
    wl = frozenset((str(e), int(d)) for e in eids for d in (-1, 1))
    rp_kw = dict(k_dq=float(raw["k_dq"]), k_imp=float(raw["k_imp"]),
                 k_is=float(raw["k_is"]), turtle_stop_atr=float(raw["turtle_stop_atr"]),
                 k_bm=float(raw["k_bm"]), whitelist=wl)
    if rp_override:
        rp_kw.update(rp_override)
    base_rp = V6Params(**rp_kw)
    rp_by_tf = None
    if rp_by_tf_override:
        rp_by_tf = {tf_k: dc_replace(base_rp, **ovr) for tf_k, ovr in rp_by_tf_override.items()}

    res = run_campaigns(
        frames, segment=segment, dev_frac=float(raw["dev_frac"]),
        equity0=300.0, risk_frac=float(raw["risk_frac"]),
        leverage_cap=float(raw["leverage_cap"]),
        taker_fee=float(raw["taker_fee"]), maker_fee=float(raw["maker_fee"]),
        slippage_per_side=float(raw["slippage_per_side"]),
        funding_per_8h=float(raw["funding_per_8h_per_side"]),
        cost_multiplier=cost_mult, contracts=contracts, params=params,
        n_fill=int(raw["n_fill"]),
        portfolio_cap_frac=float(raw["portfolio_cap_frac"]),
        router_params=base_rp, enable_mic=enable_mic,
        router_params_by_tf=rp_by_tf,
    )
    c = [t for t in res.trades if t.get("closed") and t.get("in_segment_open")]
    if not c:
        print(f"{label:52s} | N=   0")
        return res
    gr = [t["gross_pnl"] / t["risk_usdt"] for t in c if t["risk_usdt"]]
    w = [t for t in c if t["gross_pnl"] > 0]
    l = [t for t in c if t["gross_pnl"] <= 0]
    net = sum(t["net_pnl"] for t in c)
    gross = sum(t["gross_pnl"] for t in c)
    fee = sum(t["fees"] + t["funding"] + t["slippage_cost"] for t in c)
    avg_w = sum(t["gross_pnl"] / t["risk_usdt"] for t in w) / len(w) if w else 0.0
    avg_l = sum(t["gross_pnl"] / t["risk_usdt"] for t in l) / len(l) if l else 0.0
    print(f"{label:52s} | N={len(c):4d} gross={gross:+7.1f} cost={fee:5.1f} "
          f"net={net:+7.1f} | gR={sum(gr)/len(gr):+.3f} wr={len(w)/len(c):.3f} "
          f"W={avg_w:+.2f}R L={avg_l:+.2f}R")
    if show_by_tf:
        for tf_k in ("2h", "4h", "12h", "1d"):
            sub = [t for t in c if t.get("tf") == tf_k]
            if not sub:
                continue
            s_net = sum(t["net_pnl"] for t in sub)
            s_gross = sum(t["gross_pnl"] for t in sub)
            s_gr = [t["gross_pnl"] / t["risk_usdt"] for t in sub if t["risk_usdt"]]
            s_w = [t for t in sub if t["gross_pnl"] > 0]
            print(f"    └─ {tf_k:3s}: N={len(sub):4d} gross={s_gross:+7.1f} "
                  f"net={s_net:+7.1f} gR={sum(s_gr)/len(s_gr):+.3f} "
                  f"wr={len(s_w)/len(sub):.3f}")
    return res


if __name__ == "__main__":
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    syms = list(raw["symbols"])
    contracts, _ = rb.load_contracts_cache(syms)
    base = Params.from_json(CONFIG)
    params = Params(**{**vars(base), "use_balance_detector": True, "balance_quality_min": 0.35})
    htf_list = ("2h", "4h", "12h", "1d")

    cog_by, df_cog_by, df_exec_by, df_trig_by = load_all_data(syms, params, htf_list)
    mic_cache = {}

    print("\n=== 1. 最佳组合验证 & 2h 谨慎克制过滤 (在 2h+4h+12h+1d 组合内) ===")
    f_m1 = build_frames_for_map(syms, htf_list, {}, params, cog_by, df_cog_by,
                                df_exec_by, df_trig_by, mic_cache)
    f_m3c = build_frames_for_map(syms, htf_list,
                                 {"2h": None, "4h": None, "12h": 7200, "1d": 14400},
                                 params, cog_by, df_cog_by, df_exec_by, df_trig_by, mic_cache)
    f_m3e = build_frames_for_map(syms, htf_list,
                                 {"2h": 1800, "4h": None, "12h": 7200, "1d": 14400},
                                 params, cog_by, df_cog_by, df_exec_by, df_trig_by, mic_cache)

    eval_run("M1. 全同周期触发 (2h/4h/12h/1d 各自收盘)", f_m1, raw, contracts, params,
             enable_mic=False, show_by_tf=True)
    eval_run("M3c. 高阶降档 (2h/4h同周期, 12h->2h, 1d->4h)", f_m3c, raw, contracts, params,
             enable_mic=True, show_by_tf=True)
    eval_run("M3e. 2h->30m + 4h同周期 + 12h->2h + 1d->4h (无过滤)", f_m3e, raw, contracts, params,
             enable_mic=True, show_by_tf=True)

    print("\n=== 5. dev vs test 与成本梯度 (1.0x / 1.5x / 2.0x) 终局检验 ===")
    for name, f_map, em, ovr in [
        ("M1 (2h/4h/12h/1d 全同周期)", f_m1, False, None),
        ("M3c (2h/4h同周期, 12h->2h, 1d->4h)", f_m3c, True, None),
        ("M3c+2h(rot>=2)", f_m3c, True, {"2h": {"min_box_rotations": 2}}),
    ]:
        print(f"\n--- {name} ---")
        for seg in ("dev", "test", "full"):
            for cm in (1.0, 1.5, 2.0):
                if seg != "full" and cm == 2.0:
                    continue
                eval_run(f"  {seg:4s} @ {cm:.1f}x", f_map, raw, contracts, params,
                         enable_mic=em, rp_by_tf_override=ovr, segment=seg,
                         cost_mult=cm, show_by_tf=(seg == "full" and cm == 1.0))





