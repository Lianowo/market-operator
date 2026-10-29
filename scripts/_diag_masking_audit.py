"""掩蔽审计(只读诊断):逐个拆掉"用参数/过滤掩盖语义"的装置,看它到底在补什么。

候选装置(全部来自 config/strategy.json 与 src/ 的既有阈值):
  V1 min_bar_impulse_atr=1.2     —— "进场K线必须已经是大振幅"= 逼你在行情走掉一截后才追
  V2 zone_cooldown_bars=4/3      —— 用"根数"代替"同一条箱沿+同一 episode"的结构去重
  V3 min_box_rotations=3/2       —— 箱体生命周期已由 TrueBalance 的 min_swings 定义,再设一道"轮动次数"
  V4 accept_use_struct_stop=True —— 突破失效的语义是"收盘回到箱内",不是固定 2.0*ATR
  V5 invalid 锚点:ep.ex(本次被拒绝的极值) 而不是 max(z.hi, ep.ex)(箱体外沿)
  V6/V7 以上组合

判定口径:dev 与 test 同向变好、full@1.5x 不恶化、去Top10 不恶化 ⇒ 该装置在掩盖语义;
反之 ⇒ 该阈值有独立信息量(保留),报告里如实标注。
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import market_operator.events as events_mod                    # noqa: E402
from market_operator import scenario as sc                     # noqa: E402
from market_operator.campaign import run_campaigns             # noqa: E402

ORIG_REJECT = events_mod._reject


def _summ(res, eq0: float = 300.0) -> dict:
    cl = [t for t in res.trades if t.get("closed")]
    n = len(cl)
    if not n:
        return {"n": 0, "net": 0.0, "ex10": 0.0, "wr": 0.0, "pf": 0.0}
    sp = sorted([float(t["net_pnl"]) for t in cl], reverse=True)
    w = [t for t in cl if t["net_pnl"] > 0]
    lo = [t for t in cl if t["net_pnl"] <= 0]
    pf = sum(t["net_pnl"] for t in w) / max(1e-9, abs(sum(t["net_pnl"] for t in lo)))
    return {"n": n, "net": sum(sp), "ex10": sum(sp[10:]) if n > 10 else 0.0,
            "wr": len(w) / n * 100.0, "pf": pf}


def _tight_reject(eid, direction, zone, t, ts, bm, extra, edge):
    """把 invalid 从"箱体外沿"改成"本次被拒绝的极值 ep.ex"(真正的证伪价位)。"""
    ev = ORIG_REJECT(eid, direction, zone, t, ts, bm, extra, edge)
    ex = extra.get("ex")
    if ex is not None and ev.attrs.get("invalid") is not None:
        ev.attrs["invalid"] = (float(ex) + bm) if direction < 0 else (float(ex) - bm)
    return ev


def evaluate(label: str, frames, runner, rp, rpm, arm) -> None:
    st = runner.settings

    def _r(seg, cm):
        costs = st.costs.scaled(cm)
        return _summ(run_campaigns(
            frames, segment=seg, dev_frac=st.dev_frac, equity0=300.0,
            risk_frac=st.risk_frac, leverage_cap=st.leverage_cap,
            taker_fee=costs.taker_fee, maker_fee=costs.maker_fee,
            slippage_per_side=costs.slippage_per_side,
            funding_per_8h=costs.funding_per_8h,
            cost_multiplier=cm, funding_rates=costs.funding_rates,
            slippage_by_symbol=costs.slippage_by_symbol,
            contracts=runner.contracts, params=st.structure,
            n_fill=st.n_fill, portfolio_cap_frac=st.portfolio_cap_frac,
            directional_cap_frac=None, tf_cap_by_tf=dict(st.tf_cap_by_timeframe),
            capacity_mode=st.capacity_mode, arm=arm, router_params=rp,
            router_params_by_tf=rpm, enable_mic=True))

    d, t, f, x = _r("dev", 1.0), _r("test", 1.0), _r("full", 1.0), _r("full", 1.5)
    print(f"{label:34s} | dev:{d['net']:+7.2f} | test:{t['net']:+7.2f} | "
          f"full:{f['net']:+7.2f}(N={f['n']:3d},WR={f['wr']:4.1f}%,PF={f['pf']:4.2f}) | "
          f"去Top10:{f['ex10']:+7.2f} | 1.5x:{x['net']:+7.2f}")


def main() -> None:
    raw = json.loads(sc.PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    runner = sc.ScenarioRunner(tuple(raw["symbols"]), sc.PRODUCTION_CONFIG,
                               progress=False)
    st = runner.settings
    base_rp, base_rpm = st.router, dict(st.router_by_timeframe)

    def _var(**over) -> tuple:
        rp = replace(base_rp, **over)
        rpm = {k: replace(v, **over) for k, v in base_rpm.items()}
        return rp, rpm

    want = {a.upper() for a in sys.argv[1:]} or None

    def _pick(name: str) -> bool:
        return want is None or name.upper() in want

    print("=" * 132)
    print(f"{'变体':34s} | {'dev净利':>10s} | {'test净利':>10s} | "
          f"{'full@1.0x (N, WR, PF)':>30s} | {'去Top10':>10s} | {'full@1.5x':>10s}")
    print("-" * 132)
    if _pick("V0"):
        evaluate("V0 生产当前(config/strategy.json)", runner.frames, runner,
                 base_rp, base_rpm, st.arm)
    if _pick("V1"):
        evaluate("V1 拆 impulse: 1.2 -> 0.0", runner.frames, runner,
                 *_var(min_bar_impulse_atr=0.0), st.arm)
    if _pick("V2"):
        evaluate("V2 拆 cooldown: 4/3 -> 0", runner.frames, runner,
                 *_var(zone_cooldown_bars=0), st.arm)
    if _pick("V3"):
        evaluate("V3 拆 rotations: 3/3/1/2 -> 0", runner.frames, runner,
                 *_var(min_box_rotations=0), st.arm)
    if _pick("V4"):
        evaluate("V4 ACCEPT 用结构失效 (非2ATR)", runner.frames, runner,
                 *_var(accept_use_struct_stop=True), st.arm)
    if not want or want & {"V5", "V6", "V7"}:
        events_mod._reject = _tight_reject
        try:
            # 认知帧(事件)在 ScenarioRunner 构造时就已算好并缓存 —— 事后打补丁
            # 不会影响已缓存的事件流,必须**重建**才能让 V5 真正生效。
            runner2 = sc.ScenarioRunner(tuple(raw["symbols"]),
                                        sc.PRODUCTION_CONFIG, progress=False)
            st2 = runner2.settings

            def _var2(**over) -> tuple:
                rp = replace(st2.router, **over)
                rpm = {k: replace(v, **over)
                       for k, v in st2.router_by_timeframe.items()}
                return rp, rpm

            if _pick("V5"):
                evaluate("V5 invalid 锚 ep.ex (非箱体外沿)", runner2.frames,
                         runner2, st2.router, dict(st2.router_by_timeframe),
                         st2.arm)
            if _pick("V6"):
                evaluate("V6 = V4 + V5", runner2.frames, runner2,
                         *_var2(accept_use_struct_stop=True), st2.arm)
            if _pick("V7"):
                evaluate("V7 = V1+V2+V3+V4+V5 全拆", runner2.frames, runner2,
                         *_var2(min_bar_impulse_atr=0.0, zone_cooldown_bars=0,
                                min_box_rotations=0,
                                accept_use_struct_stop=True), st2.arm)
        finally:
            events_mod._reject = ORIG_REJECT


if __name__ == "__main__":
    main()
