"""A/B/C/D 四臂单变量对照诊断(a0 基线 + 单变量;预注册见
reports/prereg/20261022_abcd.md)。

臂定义:
  a0 现行 v21 口径(config/v21.json 逐项照抄,用于 parity 校验)
  A  ACCEPT×TRANSITION 准入闸:view.regime=TRANSITION 时丢弃 ACCEPT 事件
  B  TF 预算再分配:12h 0.0225->0.0075(3槽->1槽),4h 0.0225->0.0375(3槽->5槽)
  C  REJECT 前缘 maker:e3_market 市价 -> zone 前缘限价(long=zone_lo/short=zone_hi)
  D  TF_CAP 口径:mark -> 入场冻结 + 同品种净额;gross <= 1.2x cap 护栏
  D2 注册的 D 完整实现:闸门 + sizing(broker._available_stop_risk)同改冻结基
  B2 注册刻度补跑:12h 0.0225 -> 0.012(1 槽;B 实跑成 0 槽)

归因诊断(dev-only,不参与判定、不解封 test):
  A3 禁 REJECT×TRANSITION / B12 只动 12h / B4 只动 4h

地位:只读诊断(不改 config/、不改 src/)。D 通过运行期替换
campaign._outstanding_worst_case 实现(仅准入闸,不动 sizing 侧)。

用法:
    python scripts/_diag_v21_abcd.py --arms a0,A,B,C,D --segments dev --cm 1.0,1.5
    python scripts/_diag_v21_abcd.py --arms a0,A --segments test --cm 1.0,1.5
"""

from __future__ import annotations

import argparse
import csv
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
from market_operator import campaign as campaign_mod               # noqa: E402
from market_operator.campaign import (                             # noqa: E402
    ArmConfig,
    build_campaign_frame,
    run_campaigns,
)
from market_operator.events import canonical_event_id                # noqa: E402
from market_operator.broker import Broker                          # noqa: E402
from market_operator.structure import Params, Regime               # noqa: E402
from market_operator.strategy_v6 import (                          # noqa: E402
    ROUTE_LEVEL_REJECTED,
    V6Params,
    V6Router,
)
from market_operator.trigger import prepare_mic_cognition          # noqa: E402

CONFIG = ROOT / "config" / "v21.json"
OUTDIR = ROOT / "reports" / "tf_trigger_exp" / "abcd_20261022"
HTF = ("2h", "4h", "12h", "1d")
TRIG_MAP = {"2h": None, "4h": 7200, "12h": 7200, "1d": 14400}   # T0(现行)
CSV_COLS = ("trade_id", "symbol", "tf", "event_id", "regime", "direction",
            "route", "entry_mode", "fill_wait_bars", "risk_usdt", "notional",
            "used_leverage", "open_decision_ts", "open_ts", "entry",
            "exit_ts", "exit_price", "gross_pnl", "fees", "fees_maker",
            "fees_taker", "funding", "slippage_cost", "net_pnl",
            "r_multiple", "hold_hours", "reason_exit", "closed",
            "in_segment_open")



# ---------------------------------------------------------------------------
# 臂 A:ACCEPT×TRANSITION 准入闸(事件层;bar / mic 两路共用 on_bar)
# ---------------------------------------------------------------------------


class _TransitionGateRouter(V6Router):
    """regime=TRANSITION 时丢弃指定家族事件(其余原样通过)。

    家族按 canonical id 匹配(E 族规范形:BREAKOUT_ACCEPTED=ACCEPT / LEVEL_REJECTED=REJECT)。
    """

    family: str = "BREAKOUT_ACCEPTED"

    def on_bar(self, symbol, bar, bar_index, view, atr, new_events, *,
               position_open: bool, entry_resting: bool):
        if view is not None and getattr(view, "regime", None) is Regime.TRANSITION:
            kept = [e for e in new_events
                    if canonical_event_id(e.id) != self.family]
            n_sup = len(new_events) - len(kept)
            if n_sup:
                key = f"{self.family}_transition_blocked"
                self.counters[key] = self.counters.get(key, 0) + n_sup
            new_events = kept
        return super().on_bar(symbol, bar, bar_index, view, atr, new_events,
                              position_open=position_open,
                              entry_resting=entry_resting)


class AcceptTransitionGateRouter(_TransitionGateRouter):
    """臂 A:regime=TRANSITION 时丢弃 ACCEPT(BREAKOUT_ACCEPTED)事件。"""

    family = "BREAKOUT_ACCEPTED"


class RejectTransitionGateRouter(_TransitionGateRouter):
    """分解诊断 A3:regime=TRANSITION 时丢弃 REJECT(LEVEL_REJECTED)事件(非判定臂)。"""

    family = "LEVEL_REJECTED"


# ---------------------------------------------------------------------------
# 臂 C:REJECT 前缘 maker(市价决策 -> zone 前缘限价, canonical limit 通道)
# ---------------------------------------------------------------------------


class RejectFrontMakerRouter(V6Router):
    """e3_market 决策改前缘限价(long 挂 zone_lo / short 挂 zone_hi)。

    限价必须在回调方向(可能成交为 maker);无 zone 或限价在同侧 ⇒ 保持
    市价(如实记录,不静默改语义)。成交走 campaign 主路径 `kind="limit"`,
    即 broker 的 maker 费 + 无滑点限价通道。
    """

    def _route_market(self, symbol, ev, view, close, atr, route, tag):
        d = super()._route_market(symbol, ev, view, close, atr, route, tag)
        if d is None or route != ROUTE_LEVEL_REJECTED:
            return d
        lo, hi = d.meta.get("zone_lo"), d.meta.get("zone_hi")
        if lo is None or hi is None:
            self._bump("reject_front_no_zone")
            return d
        px = float(lo) if ev.direction > 0 else float(hi)
        if ev.direction > 0 and not (px < close):
            self._bump("reject_front_wrong_side")
            return d
        if ev.direction < 0 and not (px > close):
            self._bump("reject_front_wrong_side")
            return d
        self._bump("reject_front_limit")
        reason = (f"{d.intent.reason} limit_front_edge={px:.8g} "
                  f"(zone {float(lo):.8g}..{float(hi):.8g})")
        return dc_replace(
            d, kind="limit", limit_price=px,
            intent=dc_replace(d.intent, reason=reason))


# ---------------------------------------------------------------------------
# 臂 D:TF_CAP 计量口径(mark -> 入场冻结 + 同品种净额, gross<=1.2x 护栏)
# ---------------------------------------------------------------------------

D_GROSS_RAIL = 1.2


def _frozen_stop_risk(self, equity: float, symbol=None) -> float:
    """`Broker._available_stop_risk` 的冻结基版本(臂 D2 的 sizing 半边)。

    唯一改动:`mark = last_mark.get(sym, entry)` -> `ref = pos.entry`(三处)。
    TF 池 / 全局池 / 提前返回顺序与权限判定全部照抄 canonical,**不动公式**。
    语义:容量按"入场即定的风险"计,不随 mark 漂移 —— 与闸门侧
    `_net_frozen_worst_case` 同一计量基(入场冻结),故 D2 = 同一变量作用于两侧。
    """
    if self.tf_stop_risk_frac_by_tf and symbol and "@" in symbol:
        tf_lbl = symbol.rsplit("@", 1)[1]
        tf_cap = self.tf_stop_risk_frac_by_tf.get(tf_lbl)
        if tf_cap is not None:
            outstanding_tf = 0.0
            suffix = f"@{tf_lbl}"
            for sym, pos in self.positions.items():
                if not sym.endswith(suffix):
                    continue
                if pos.invalid is None:
                    return 0.0
                ref = pos.entry
                outstanding_tf += pos.qty * max(0.0, pos.direction * (ref - pos.invalid))
            avail_tf = max(0.0, equity * tf_cap - outstanding_tf)
            if self.portfolio_stop_risk_frac is None:
                return avail_tf
            outstanding_all = 0.0
            for sym, pos in self.positions.items():
                if pos.invalid is None:
                    return 0.0
                ref = pos.entry
                outstanding_all += pos.qty * max(
                    0.0, pos.direction * (ref - pos.invalid))
            avail_all = max(0.0, equity * self.portfolio_stop_risk_frac - outstanding_all)
            return min(avail_tf, avail_all)
    if self.portfolio_stop_risk_frac is None:
        return float("inf")
    outstanding = 0.0
    for sym, pos in self.positions.items():
        if pos.invalid is None:
            return 0.0
        ref = pos.entry
        outstanding += pos.qty * max(0.0, pos.direction * (ref - pos.invalid))
    return max(0.0, equity * self.portfolio_stop_risk_frac - outstanding)


def _net_frozen_worst_case(broker, tf_filter=None) -> float:
    """`max(净敞口冻结最坏情景, 毛额冻结/1.2)`;与 cap*equity 比较。

    冻结 = qty·|entry−invalid|(入场即定,不随 mark 漂移);
    净额 = 同品种(跨 TF)按方向带符号相加后的绝对值 —— 自对冲部分释放容量;
    毛额护栏保证 gross 不超过 1.2×cap(cap 比较对象仍为单标量)。
    """
    suffix = f"@{tf_filter}" if tf_filter else None
    net: dict = {}
    gross = 0.0
    for sym, pos in broker.positions.items():
        if suffix and not sym.endswith(suffix):
            continue
        if pos.invalid is None:
            return float("inf")
        frozen = pos.qty * abs(pos.entry - float(pos.invalid))
        mark = broker.last_mark.get(sym, pos.entry)
        close_fee = pos.qty * mark * (broker.taker_fee + broker.slippage)
        risk = frozen + close_fee
        gross += risk
        base = sym.rsplit("@", 1)[0] if "@" in sym else sym
        net[base] = net.get(base, 0.0) + int(pos.direction) * risk
    net_abs = sum(abs(v) for v in net.values())
    return max(net_abs, gross / D_GROSS_RAIL)



# ---------------------------------------------------------------------------
# 数据/参数装配(与 _diag_v21_tf_sweep.Ctx 同口径)
# ---------------------------------------------------------------------------


class Ctx:
    def __init__(self, only=None) -> None:
        self.cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        want = list(self.cfg["symbols"]) if only is None else list(only)
        syms = [s for s in want
                if (ROOT / "data" / "raw" / f"{s}_1h.csv").exists()]
        missing = [s for s in want if s not in syms]
        if missing:
            print(f"[warn] 无 1h 原始数据, 本轮剔除: {missing}", flush=True)
        if not syms:
            raise SystemExit("no symbol data available")
        self.syms = syms
        self.contracts, _src = rb.load_contracts_cache(syms)
        base = Params.from_json(CONFIG)
        self.params = Params(**{**vars(base), "use_balance_detector": True,
                                "balance_quality_min": 0.35})
        t0 = time.time()
        (self.cog_by, self.df_cog_by, self.df_exec_by,
         self.df_trig_by) = diag.load_all_data(syms, self.params, tf_list=HTF)
        print(f"[prep] 数据就绪 {syms} ({time.time() - t0:.1f}s)", flush=True)

        cfg = self.cfg
        wl = frozenset((str(e), int(d)) for e, d in cfg["whitelist"])
        self.base_rp = V6Params(
            k_dq=float(cfg["k_dq"]), k_imp=float(cfg["k_imp"]),
            k_is=float(cfg["k_is"]),
            turtle_stop_atr=float(cfg["turtle_stop_atr"]),
            k_bm=float(cfg["k_bm"]), whitelist=wl,
            **cfg.get("router_params", {}))
        self.rp_by_tf = {
            tf_k: dc_replace(self.base_rp, **ovr)
            for tf_k, ovr in cfg["router_params_by_tf"].items()}
        self.arm = ArmConfig(**cfg["arm_config"])
        self.frames = self._frames()

    def _mic_for(self, sym, tf, trig_sec):
        obs_sec = agg.TF_HOURS[tf] * 3600
        return prepare_mic_cognition(
            self.df_cog_by[(sym, tf)], self.df_trig_by[sym][trig_sec],
            self.params, sym, obs_tf_seconds=obs_sec,
            trigger_tf_seconds=trig_sec, enable_e7=True)

    def _frames(self) -> dict:
        out = {}
        for sym in self.syms:
            for tf in HTF:
                trig_sec = TRIG_MAP[tf]
                mic = None
                if trig_sec is not None:
                    mic = self._mic_for(sym, tf, trig_sec)
                f = build_campaign_frame(self.cog_by[(sym, tf)],
                                         self.df_exec_by[(sym, tf)], tf,
                                         mic_cog=mic)
                out[f.key] = f
        return out

    # -- run ---------------------------------------------------------------
    def run(self, *, segment="dev", cm=1.0, router_cls=None, tfcap=None,
            cap_basis="mark", frozen_sizing=False):
        cfg = self.cfg
        tfcap = dict(cfg["tf_cap_by_tf"]) if tfcap is None else tfcap
        orig = campaign_mod._outstanding_worst_case
        orig_sr = Broker._available_stop_risk
        try:
            if cap_basis == "net_frozen":
                campaign_mod._outstanding_worst_case = _net_frozen_worst_case
            if frozen_sizing:
                Broker._available_stop_risk = _frozen_stop_risk
            return run_campaigns(
                self.frames, segment=segment, dev_frac=float(cfg["dev_frac"]),
                equity0=300.0, risk_frac=float(cfg["risk_frac"]),
                leverage_cap=float(cfg["leverage_cap"]),
                taker_fee=float(cfg["taker_fee"]),
                maker_fee=float(cfg["maker_fee"]),
                slippage_per_side=float(cfg["slippage_per_side"]),
                funding_per_8h=float(cfg["funding_per_8h_per_side"]),
                cost_multiplier=cm, contracts=self.contracts,
                params=self.params, n_fill=int(cfg["n_fill"]),
                portfolio_cap_frac=float(cfg["portfolio_cap_frac"]),
                tf_cap_by_tf=tfcap, arm=self.arm,
                router_params=self.base_rp, enable_mic=True,
                router_params_by_tf=self.rp_by_tf,
                router_factory=router_cls)
        finally:
            campaign_mod._outstanding_worst_case = orig
            Broker._available_stop_risk = orig_sr



# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


def metrics(res, equity0: float = 300.0) -> dict:
    c = [t for t in res.trades if t.get("closed") and t.get("in_segment_open")]
    gross = sum(t["gross_pnl"] for t in c)
    net = sum(t["net_pnl"] for t in c)
    cost = sum(t["fees"] + t["funding"] + t["slippage_cost"] for t in c)
    wins = [t for t in c if t["gross_pnl"] > 0]
    gw = sum(t["gross_pnl"] for t in wins)
    gl = -sum(t["gross_pnl"] for t in c if t["gross_pnl"] <= 0)
    peak, dd = -1e18, 0.0
    for row in res.equity_rows:
        e = float(row[1])
        peak = max(peak, e)
        if peak > 0:
            dd = max(dd, (peak - e) / peak)
    by_tf = {}
    for tf in HTF:
        sub = [t for t in c if t.get("tf") == tf]
        by_tf[tf] = {"n": len(sub),
                     "net": round(sum(t["net_pnl"] for t in sub), 4)}
    by_route: dict = {}
    for t in c:
        k = f"{t.get('event_id')}×{t.get('regime')}"
        b = by_route.setdefault(k, {"n": 0, "net": 0.0})
        b["n"] += 1
        b["net"] += t["net_pnl"]
    return {
        "n": len(c), "gross": round(gross, 4), "cost": round(cost, 4),
        "net": round(net, 4), "eq": round(equity0 + net, 4),
        "wr": round(len(wins) / len(c), 4) if c else 0.0,
        "pf": round(gw / gl, 4) if gl > 0 else None,
        "dd_pct": round(dd * 100.0, 4),
        "limit_entries": sum(1 for t in c if t.get("entry_mode") == "limit"),
        "by_tf": by_tf,
        "by_event_regime": {k: {"n": v["n"], "net": round(v["net"], 4)}
                            for k, v in sorted(by_route.items())},
        "counters": {k: v for k, v in getattr(res, "counters", {}).items() if v},
        "trades": c,
    }


def dump_trades(path: Path, trades: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(CSV_COLS), extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow({k: t.get(k) for k in CSV_COLS})


# ---------------------------------------------------------------------------
# 臂装配 / 主流程
# ---------------------------------------------------------------------------

ARMS = ("a0", "A", "B", "C", "D")


def arm_spec(name: str, ctx: Ctx) -> dict:
    base_tfcap = dict(ctx.cfg["tf_cap_by_tf"])
    if name == "a0":
        return {}
    if name == "A":
        return {"router_cls": AcceptTransitionGateRouter}
    if name == "A3":                     # 分解诊断(非判定臂)
        return {"router_cls": RejectTransitionGateRouter}
    if name == "B":
        cap = dict(base_tfcap)
        cap["12h"] = 0.0075      # 3 槽 -> 1 槽(实际 0 槽:候选最坏情景 > 单槽)
        cap["4h"] = 0.0375       # 3 槽 -> 5 槽(总预算 0.09 不变)
        return {"tfcap": cap}
    if name == "B12":                    # 分解诊断(非判定臂):只动 12h
        cap = dict(base_tfcap)
        cap["12h"] = 0.0075
        return {"tfcap": cap}
    if name == "B4":                     # 分解诊断(非判定臂):只动 4h
        cap = dict(base_tfcap)
        cap["4h"] = 0.0375
        return {"tfcap": cap}
    if name == "B2":                     # 注册刻度补跑:12h -> 1 槽(0.012)
        cap = dict(base_tfcap)
        cap["12h"] = 0.012
        return {"tfcap": cap}
    if name == "C":
        return {"router_cls": RejectFrontMakerRouter}
    if name == "D":
        return {"cap_basis": "net_frozen"}
    if name == "D2":                     # 注册的 D 完整实现:闸门 + sizing 同改冻结基
        return {"cap_basis": "net_frozen", "frozen_sizing": True}
    raise SystemExit(f"unknown arm {name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--segments", default="dev")
    ap.add_argument("--cm", default="1.0,1.5")
    ap.add_argument("--symbols", default="",
                    help="逗号分隔;缺省 = config symbols 全量(缺数据者自动剔除)")
    ap.add_argument("--out", default=str(OUTDIR))
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    only = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    ctx = Ctx(only=only)
    segs = [s.strip() for s in args.segments.split(",") if s.strip()]
    cms = [float(x) for x in args.cm.split(",") if x.strip()]
    print(f"{'arm':4s} {'seg':5s} {'x':>4s} | {'N':>4s} {'gross':>8s} "
          f"{'cost':>7s} {'net':>8s} | {'wr':>5s} {'PF':>5s} {'DD%':>6s} "
          f"{'lim':>4s} | per-TF net", flush=True)
    for name in [a.strip() for a in args.arms.split(",") if a.strip()]:
        spec = arm_spec(name, ctx)
        for seg in segs:
            for cm in cms:
                t0 = time.time()
                res = ctx.run(segment=seg, cm=cm, **spec)
                m = metrics(res)
                tf_txt = " ".join(f"{tf}={m['by_tf'][tf]['net']:+.1f}"
                                  for tf in HTF)
                pf = m["pf"] if m["pf"] is not None else float("nan")
                print(f"{name:4s} {seg:5s} {cm:4.1f} | {m['n']:4d} "
                      f"{m['gross']:+8.1f} {m['cost']:7.1f} {m['net']:+8.1f} "
                      f"| {m['wr']:5.3f} {pf:5.2f} {m['dd_pct']:6.2f} "
                      f"{m['limit_entries']:4d} | {tf_txt} "
                      f"({time.time() - t0:.0f}s)", flush=True)
                stem = f"{name}__{seg}__{cm:g}"
                payload = {k: v for k, v in m.items() if k != "trades"}
                (outdir / f"metrics_{stem}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                dump_trades(outdir / f"trades_{stem}.csv", m["trades"])
    print(f"\n[out] {outdir}", flush=True)


if __name__ == "__main__":
    main()

