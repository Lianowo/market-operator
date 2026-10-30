"""突破进入平衡箱(box_reentry_enabled)与根治 ACCEPT|LONG 移除 0.35 权重 全量复现实验。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from market_operator import scenario as sc                    # noqa: E402
from market_operator.replay import prepare_cognition          # noqa: E402
import _diag_accept_watch as daw                              # noqa: E402


def main() -> None:
    raw = json.loads(sc.PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    syms = tuple(raw["symbols"])
    runner0 = sc.ScenarioRunner(syms, sc.PRODUCTION_CONFIG, progress=False)
    base_st = runner0.settings

    mkt_cache: dict = {}

    def _get_mkt(params):
        key = (params.box_reentry_enabled, params.box_reentry_max_bars,
               params.accept_strict_progression, params.accept_max_ext_atr,
               params.mic_r3_trap)
        if key not in mkt_cache:
            mkt = sc.MarketData(
                symbols=runner0.market.symbols, cognition={},
                cognition_frame=runner0.market.cognition_frame,
                execution_frame=runner0.market.execution_frame,
                trigger_frame=runner0.market.trigger_frame,
            )
            for (sym, tf), df in runner0.market.cognition_frame.items():
                mkt.cognition[(sym, tf)] = prepare_cognition(df, params, sym, enable_e7=True)
            mkt_cache[key] = mkt
        return mkt_cache[key]

    variants = (
        ("V11 不交易突破进入平衡箱 (box_reentry=False)",
         {"box_reentry_enabled": False, "accept_strict_progression": False}, {}),
        ("C1 ★ 开放突破进入平衡箱 (生产: box_reentry=True)",
         {"box_reentry_enabled": True, "accept_strict_progression": False}, {}),
        ("C3 开放突破进入平衡箱 + 破位严苛推进",
         {"box_reentry_enabled": True, "accept_strict_progression": True}, {}),
        ("C4 开放突破进入平衡箱 + 破位严苛推进 + 对侧锁利",
         {"box_reentry_enabled": True, "accept_strict_progression": True},
         {"reentry_use_box_target": True}),
    )

    print("=" * 165)
    print(f"{'变体':48s} | {'dev':>8s} | {'test':>8s} | "
          f"{'full@1.0x (N, WR, PF)':>27s} | {'去Top10':>8s} | {'1.5x':>8s} | "
          f"{'ACC多(N/净)':>13s} | {'ACC空(N/净)':>13s}")
    print("-" * 165)
    for label, s_kw, r_kw in variants:
        st_i = daw._with_overrides(base_st, struct_kw=s_kw, router_kw=r_kw)
        mkt_i = _get_mkt(st_i.structure)
        frames_i = sc.build_frames(mkt_i, st_i, sc.COGNITION_TIMEFRAMES)
        d = daw._summ(daw._run(frames_i, runner0, st_i, "dev"))
        t = daw._summ(daw._run(frames_i, runner0, st_i, "test"))
        f_res = daw._run(frames_i, runner0, st_i, "full")
        f = daw._summ(f_res)
        x = daw._summ(daw._run(frames_i, runner0, st_i, "full", 1.5))
        ac = daw._accept_split(f_res)
        print(f"{label:48s} | {d['net']:+8.2f} | {t['net']:+8.2f} | "
              f"{f['net']:+8.2f}({f['n']:3d},{f['wr']:4.1f}%,{f['pf']:4.2f}) | "
              f"{f['ex10']:+8.2f} | {x['net']:+8.2f} | "
              f"{ac['l_n']:2d}/{ac['l_net']:+7.2f}U | {ac['s_n']:2d}/{ac['s_net']:+7.2f}U")


if __name__ == "__main__":
    main()
