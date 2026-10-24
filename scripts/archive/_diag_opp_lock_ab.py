"""v21 对侧锁利口径 A/B(**v21.6 存档诊断;口径已被 v21.7 回吐预算取代**)。

历史背景:锁利只在**收盘**裁决,而收盘价常已走过 `opp_lock_px`(entry→target
行程比例)算出的位置 ⇒ 计划口径退化成"等回踩"的止盈单,实际锁到的 R 远小于
名义值。本脚本用真实数据量化了当时的两件事:行情锚定(`opp_lock_live_px`,
ref ∓ giveback_r·R)抬高了多少"实际锁到的 R",以及 giveback_r 0/0.25/0.5/1.0
对净利的影响 —— 结论(见 reports/opp_lock_ab.md 与 docs/v21-protocol.md §5):
R 单位的第二把尺子与行程比例互相遮盖,0.25R 在 dev/test 均为负,已删除。

现在锁利只有一个参数 `reject_opp_lock_frac`(回吐预算 =(1−|frac|)·span),
故本脚本的臂改为 frac 扫描;重跑得到的是 v21.7 语义下的读数,与
`reports/opp_lock_ab.md`(v21.6)不可直接比较。
对照臂 `arm=None` = 旧 §8.2 触及减仓 50%(无锁利)。

口径:M3c 帧(2h/4h 同周期,12h→2h,1d→4h)、full 段、cost 1.0x(E1/E3 家族)。

    python scripts/archive/_diag_opp_lock_ab.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_backtest_v6 as rb                                   # noqa: E402
from market_operator import aggregate as agg                    # noqa: E402
from market_operator import data as mo_data                     # noqa: E402
from market_operator.backtest_v6 import prepare_cognition       # noqa: E402
from market_operator.campaign import (                          # noqa: E402
    ArmConfig, build_campaign_frame, run_campaigns,
)
from market_operator.position import PositionManager            # noqa: E402
from market_operator.trigger import prepare_mic_cognition        # noqa: E402

CONFIG = ROOT / "config" / "canonical.json"
REPORT = ROOT / "reports" / "opp_lock_ab.md"

LOCK_STATS = {"lock": 0, "reject": 0, "locked_r": [],
              "plan_r": [], "live_r": [], "dist_r": []}
_orig_exit = PositionManager._exit
_NUM = r"(-?[\d.]+(?:e[+-]?\d+)?|n/a)"


def _num(txt):                                     # noqa: ANN001
    return None if txt == "n/a" else float(txt)


def _exit_hook(self, pos, ts, ref, reason):        # noqa: ANN001
    """出场即快照 pos.log:统计锁利推进次数/实际锁到的 R(诊断用)。

    v21.7 日志形如 `v21|opp_lock|峰值=… 预算=… 回吐=… ref=… invalid A->B`,
    "计划/行情"两口径已合并为单一回吐预算 ⇒ 这里只统计实际落点、保护侧距离
    与回吐占预算的比例。
    """
    if any(s.startswith("v21|opp_lock_armed|") for s in pos.log):
        LOCK_STATS["lock"] += 1
    if any("lock_giveback_exhausted" in s for s in pos.log):
        LOCK_STATS["reject"] += 1
    locked = [s for s in pos.log if "v21|opp_lock|" in s]
    if locked:
        d, e, R = pos.direction, pos.entry, pos.initial_risk
        LOCK_STATS["locked_r"].append(d * (pos.invalid - e) / R)
        m = re.search(rf"预算={_NUM} 回吐={_NUM} ref={_NUM} "
                      rf"invalid [\d.]+->([\d.]+)", locked[-1])
        if m:
            budget, giveback = _num(m.group(1)), _num(m.group(2))
            if budget is not None and giveback is not None:
                # 保护侧距离(budget − 回吐)与回吐占预算的比例
                LOCK_STATS["plan_r"].append((budget - giveback) / R)
                LOCK_STATS["live_r"].append(d * (float(m.group(4)) - e) / R)
                LOCK_STATS["dist_r"].append(giveback / budget if budget else 0.0)
    return _orig_exit(self, pos, ts, ref, reason)


def build_frames(symbols, params, tf_list, trigger_map, cache):
    """按 v21 生产口径建帧(触发周期来自 config/v21.json trigger_tf_by_htf)。"""
    trig_sec_by_tf = {"2h": 7200, "4h": 14400, "12h": 43200, "1d": 86400}
    frames = {}
    for sym in symbols:
        df_1h = mo_data.load(sym)
        df_map = {tf: (df_1h if tf == "1h" else agg.load_tf(sym, tf))
                  for tf in ("1h", "2h", "4h", "12h", "1d")}
        for tf in tf_list:
            cog = prepare_cognition(df_map[tf], params, sym, enable_e7=True)
            trig_sec = trigger_map.get(tf)
            mic = None
            if trig_sec is not None:
                ckey = (sym, tf, trig_sec)
                if ckey not in cache:
                    trig_tf = {7200: "2h", 14400: "4h"}[int(trig_sec)]
                    cache[ckey] = prepare_mic_cognition(
                        df_map[tf], df_map[trig_tf], params, sym,
                        obs_tf_seconds=trig_sec_by_tf[tf],
                        trigger_tf_seconds=int(trig_sec), enable_e7=True)
                mic = cache[ckey]
            f = build_campaign_frame(cog, df_1h, tf, mic_cog=mic)
            frames[f.key] = f
        print(f"[prep] {sym} 帧就绪", flush=True)
    return frames


def run_arm(label, frames, raw_can, raw_v21, contracts, params, rp_base, rp_by_tf,
            arm, segment="full"):
    LOCK_STATS.update({"lock": 0, "reject": 0, "locked_r": [], "plan_r": [],
                       "live_r": [], "dist_r": []})
    t0 = time.time()
    res = run_campaigns(
        frames, segment=segment, dev_frac=float(raw_can["dev_frac"]),
        equity0=300.0, risk_frac=float(raw_can["risk_frac"]),
        leverage_cap=float(raw_can["leverage_cap"]),
        taker_fee=float(raw_can["taker_fee"]),
        maker_fee=float(raw_can["maker_fee"]),
        slippage_per_side=float(raw_can["slippage_per_side"]),
        funding_per_8h=float(raw_can["funding_per_8h_per_side"]),
        cost_multiplier=1.0, contracts=contracts, params=params,
        n_fill=int(raw_can["n_fill"]),
        portfolio_cap_frac=float(raw_can["portfolio_cap_frac"]),
        router_params=rp_base, router_params_by_tf=rp_by_tf,
        tf_cap_by_tf=raw_v21.get("small_account_compounding", {})
                                 .get("tf_cap_by_tf"),
        enable_mic=bool(any(f.mic is not None for f in frames.values())),
        arm=arm)
    c = [t for t in res.trades if t.get("closed") and t.get("in_segment_open")]
    gr = [t["gross_pnl"] / t["risk_usdt"] for t in c if t.get("risk_usdt")]
    w = [t for t in c if t["gross_pnl"] > 0]
    lr = LOCK_STATS["locked_r"]
    def _mean(k):                                   # noqa: ANN202
        v = LOCK_STATS[k]
        return (sum(v) / len(v)) if v else 0.0
    row = {"label": label, "n": len(c), "net": sum(t["net_pnl"] for t in c),
           "gross": sum(t["gross_pnl"] for t in c),
           "gR": (sum(gr) / len(gr)) if gr else 0.0,
           "wr": (len(w) / len(c)) if c else 0.0,
           "lock_n": LOCK_STATS["lock"], "reject_n": LOCK_STATS["reject"],
           "lock_r": (sum(lr) / len(lr)) if lr else 0.0,
           "plan_r": _mean("plan_r"), "live_r": _mean("live_r"),
           "dist_r": _mean("dist_r"),
           "secs": round(time.time() - t0, 1)}
    print(f"{label:32s} N={row['n']:4d} net={row['net']:+8.2f} "
          f"gross={row['gross']:+8.2f} gR={row['gR']:+.3f} wr={row['wr']:.3f} "
          f"lock={row['lock_n']:4d} exhausted={row['reject_n']:3d} "
          f"lockedR={row['lock_r']:+.2f} 保护侧R={row['plan_r']:+.2f} "
          f"实际R={row['live_r']:+.2f} 回吐/预算={row['dist_r']:.3f} "
          f"({row['secs']}s)", flush=True)
    return row


def main() -> None:
    PositionManager._exit = _exit_hook
    sys.path.insert(0, str(ROOT / "scripts"))
    import live_runner as lr
    raw_can = json.loads(CONFIG.read_text(encoding="utf-8"))
    raw_v21, params, rp_base, rp_by_tf = lr.load_v21_params()
    syms = [s for s in raw_v21["symbols"]
            if (ROOT / "data" / "raw" / f"{s}_1h.csv").exists()]
    contracts, _ = rb.load_contracts_cache(syms)
    print(f"[cfg] v21 生产口径 符号={syms} tf={raw_v21['tf_list']} "
          f"触发={raw_v21['trigger_tf_by_htf']}", flush=True)
    frames = build_frames(syms, params, raw_v21["tf_list"],
                          raw_v21["trigger_tf_by_htf"], {})

    arms = [
        ("arm=None(旧§8.2 减仓50%)", None),
        ("锁利 frac=0.25(预算 ¾span)", ArmConfig(reject_opp_lock_frac=-0.25)),
        ("锁利 frac=0.50(预算 ½span)", ArmConfig(reject_opp_lock_frac=-0.50)),
        ("锁利 frac=0.75(预算 ¼span)", ArmConfig(reject_opp_lock_frac=-0.75)),
        ("锁利 frac=1.00(触价即平)", ArmConfig(reject_opp_lock_frac=-1.00)),
    ]
    rows = []
    for seg in ("dev", "test", "full"):
        for lb, a in arms:
            r = run_arm(f"{lb} [{seg}]", frames, raw_can, raw_v21, contracts,
                        params, rp_base, rp_by_tf, a, segment=seg)
            r["seg"] = seg
            r["arm"] = lb
            rows.append(r)

    lines = ["# v21 对侧锁利口径 A/B(生产口径 / 1.0x)", "",
             f"符号: {', '.join(syms)};tf={raw_v21['tf_list']};"
             f"触发={raw_v21['trigger_tf_by_htf']};"
             "组合口径 = canonical(equity0=300, risk_frac/portfolio_cap/n_fill 同源)",
             "",
             "## 1. 净值/胜率/期望(按段)", "",
             "| arm | seg | N | net | gross | gR | wr | 锁利次数 | 闸门拒绝 |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(
            f"| {r['arm']} | {r['seg']} | {r['n']} | {r['net']:+.2f} | "
            f"{r['gross']:+.2f} | {r['gR']:+.3f} | {r['wr']:.3f} | "
            f"{r['lock_n']} | {r['reject_n']} |")
    lines += ["", "## 2. 同一锁利事件的两种口径对照(full 段,与权益路径无关)", "",
              "| arm | 锁利次数 | 落地锁住R | 计划口径R | 行情口径R | 落地价距现价R |",
              "|---|---:|---:|---:|---:|---:|"]
    for r in rows:
        if r["seg"] != "full":
            continue
        lines.append(
            f"| {r['arm']} | {r['lock_n']} | {r['lock_r']:+.2f} | "
            f"{r['plan_r']:+.2f} | {r['live_r']:+.2f} | {r['dist_r']:+.2f} |")
    lines += ["", "口径:锁利次数 = 出场时 pos.log 含落地锁利行;落地锁住R = "
              "出场时 d·(invalid−entry)/initial_risk(含后续 trail);计划口径R / "
              "行情口径R = 同一锁利事件两种口径各自的锁住 R;落地价距现价R = "
              "d·(ref−锁利价)/initial_risk(越大 = 越像\"等回踩的止盈单\")。",
              "", "注:锁利事件在 3 币 x 4 TF 样本里只有个位数,net 差异主要由"
              "组合容量路径(不同 arm 的 N 不同)贡献,**不足以据此调参**;"
              "第 2 张表才是口径优劣的硬证据(同一事件、与路径无关)。"]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"-> {REPORT}")


if __name__ == "__main__":
    main()
