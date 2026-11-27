"""PAPER 端到端冒烟(实盘接入件;不发任何真单)。

流程:真实拉取 BTCUSDT warmup 窗口 2H 帧 → 认知帧装配(canonical replay;bar
事件流 == prepare_cognition 硬断言)→ 再拉 30 根真实 2H bar 逐根复放 → 状态
断点重启(round-trip)同结果断言 → PAPER 对账消息样本打印(live 私信接口只读
探测:balance/positions 合法即可)+ tg 连接测试(真实发一条 alert)。

用法:  python scripts/live_paper_check.py [--config config/live.json]
产物:  打印报告(Warmup/Replay/Restart/Reconcile/TG 五段;退出码非零即失败)。
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from market_operator import live_support, ops, paths                    # noqa: E402
from market_operator.bitget_live import BitgetLiveClient                # noqa: E402
from market_operator.events import EventDetector                        # noqa: E402
from market_operator.replay import prepare_cognition                    # noqa: E402
from market_operator.structure import Params                            # noqa: E402
from market_operator.tg import TelegramNotifier                         # noqa: E402

import live_runner as lr                                                # noqa: E402

REPLAY_BAR_N = 30


class ReplayClient:
    """录像 client:history_candles 按录制窗口回放;仅白名单只读接口委派真实 client。"""

    READ_ONLY: frozenset[str] = frozenset({
        "tickers",
        "contracts",
        "rwa_symbols",
        "funding_rate_history",
        "balance",
        "account_bills",
        "positions",
        "position_mode",
        "orders_pending",
        "tpsl_orders_pending",
        "tpsl_order_history",
        "recent_fills",
        "order_detail",
        "query_string",
        "product_type",
        "base_url",
        "api_errors",
    })

    def __init__(
        self,
        real: BitgetLiveClient,
        tape: Mapping[str, list[tuple[int, float, float, float, float, float]]],
        warmup_end: int,
    ) -> None:
        self._real = real
        self._tape = tape
        self._warmup_end = warmup_end

    def history_candles(
        self,
        symbol: str,
        granularity: str = "2H",
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 200,
    ) -> list[tuple[int, float, float, float, float, float]]:
        _ = granularity
        warm = [b for b in self._tape[symbol] if b[0] <= self._warmup_end]
        if start_ms is None:
            out = [b for b in warm if end_ms is None or b[0] < end_ms]
            return out[-limit:]
        out = [b for b in self._tape[symbol] if b[0] >= start_ms and (end_ms is None or b[0] < end_ms)]
        return out[:limit] if end_ms is None else out[-limit:]

    def universe(
        self,
        thresholds: Any = None,
        tickers: Any = None,
        contracts: Any = None,
        rwa_no_volume_filter: bool | None = None,
    ) -> list[str]:
        _ = (thresholds, tickers, contracts, rwa_no_volume_filter)
        return ["BTCUSDT"]

    def __getattr__(self, name: str) -> Any:
        if name in self.READ_ONLY:
            return getattr(self._real, name)

        def _forbid(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError(f"PAPER 冒烟出现非只读调用 {name}(fail-closed 违例)")

        return _forbid


class CollectTg:
    """tg 收集器(冒烟 printed 样本;真实连接测试单独走真 TelegramNotifier)。"""

    def __init__(self) -> None:
        self.messages: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("send_"):
            def _send(*args: Any, **kwargs: Any) -> bool:
                self.messages.append((name, args, kwargs))
                return True

            return _send
        raise AttributeError(name)


def build_runner(
    tmp: Path,
    client: Any,
    tg: Any,
    cfg: dict[str, Any],
    contracts: dict[str, Any],
    contract_raw: dict[str, Any] | None = None,
) -> lr.LiveRunner:
    return lr.LiveRunner(
        "paper",
        cfg,
        client,
        tg,
        equity0=float(cfg["paper"]["equity0"]),
        contracts=contracts,
        contract_raw=contract_raw or {},
        state_path=tmp / "state.json",
        journal_dir=tmp / "logs",
        symbols=("BTCUSDT",),
        universe_enabled=False,
    )


def _fetch_warmup_and_future(
    real: BitgetLiveClient,
    sym: str,
    want: int,
) -> tuple[list[tuple[int, float, float, float, float, float]], list[tuple[int, float, float, float, float, float]], list[tuple[int, float, float, float, float, float]]]:
    have: dict[int, tuple[int, float, float, float, float, float]] = {}
    end_ms: int | None = None
    guard = 0
    while len(have) < want and guard < (want // 200) + 3:
        page = real.history_candles(sym, "2H", end_ms=end_ms, limit=200)
        fresh = [b for b in page if b[0] not in have]
        if not fresh:
            break
        for bar in fresh:
            have[bar[0]] = bar
        end_ms = fresh[0][0]
        guard += 1
    all_bars = [have[t] for t in sorted(have)][-want:]
    assert len(all_bars) == want, f"拉取不足:{len(all_bars)} < {want}"
    return all_bars, all_bars[:-REPLAY_BAR_N], all_bars[-REPLAY_BAR_N:]


def _verify_cognition_parity(cs: live_support.CampaignState, params: Params, sym: str) -> None:
    import pandas as pd

    df_cog = pd.DataFrame(cs.cog_bars, columns=["ts", "open", "high", "low", "close", "volume"])
    ref = prepare_cognition(df_cog, params, sym, enable_e7=True)
    got = _events_of(cs, params)
    want = {i: [(e.id, e.direction) for e in evs] for i, evs in ref.events_by.items()}
    diff = {k: (got.get(k), want.get(k)) for k in set(got) | set(want) if got.get(k) != want.get(k)}
    assert got == want, f"replay bar 事件流 != prepare_cognition: {diff}"
    print(
        f"   [断言] replay bar 事件流 == prepare_cognition(全 {len(cs.cog_bars)} 根,"
        f"{sum(len(v) for v in want.values())} 事件)✔"
    )


def _events_of(cs: live_support.CampaignState, params: Params) -> dict[int, list[tuple[str, int]]]:
    """独立重放 detection(纯检测器,确定性)并与 prepare_cognition 逐 bar 对齐。"""
    det = EventDetector(params, enable_e7=True)
    out: dict[int, list[tuple[str, int]]] = {}
    for idx, bar in enumerate(cs.cog_bars):
        emitted = det.update(bar)
        if emitted:
            out[idx] = [(e.id, e.direction) for e in emitted]
    return out


def main(argv: Sequence[str] | None = None) -> None:
    paths.soften_stdio()
    ap = argparse.ArgumentParser(prog="live_paper_check")
    ap.add_argument("--config", default=str(ROOT / "config" / "live.json"))
    args = ap.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    secrets = ops.load_secrets(cfg)
    real = BitgetLiveClient(secrets)
    tmp = paths.runtime_dir() / "live" / "paper_check"
    tmp.mkdir(parents=True, exist_ok=True)
    for p in (tmp / "state.json", tmp / "_probe_state.json"):
        p.unlink(missing_ok=True)
    if (tmp / "logs").exists():
        for p in (tmp / "logs").glob("live_*.jsonl"):
            p.unlink()

    sym = "BTCUSDT"
    print(
        f"== [1/5] Warmup 认知帧({sym} real 2H,canonical replay;warmup "
        f"窗口尾部留 {REPLAY_BAR_N} 根作'未来') =="
    )
    want = int(cfg["cognition"]["warmup_trigger_bars"]) + REPLAY_BAR_N
    all_bars, warmup_bars, future = _fetch_warmup_and_future(real, sym, want)
    warmup_end = warmup_bars[-1][0]
    client = ReplayClient(real, {sym: all_bars}, warmup_end=warmup_end)
    contracts, contract_raw = live_support.load_contracts_live(real, cfg.get("symbols_core") or [sym])
    tg2 = CollectTg()
    runner = build_runner(tmp, client, tg2, cfg, contracts, contract_raw)
    cs = runner._build_all_tf_campaigns(sym)
    runner.campaigns[sym] = cs
    runner.symbols_pending.remove(sym)
    assert cs.trig_bars[-1][0] == warmup_end, "warmup 窗越界(防前视破)"
    print(
        f"   认知 bar {len(cs.cog_bars)} 根;2H trigger {len(cs.trig_bars)} 根;"
        f"末 trigger 标签 {warmup_end} (多周期并发实例: {len(runner.extra_campaigns) + 1} 个)"
    )
    _verify_cognition_parity(cs, runner.params, sym)

    print(f"== [2/5] 复放 {REPLAY_BAR_N} 根真实 2H bar(paper 虚拟经纪) ==")
    t_before = time.time()
    c0 = dict(runner.counters)
    for bar in future:
        runner.step(bar[0] + 1)
    d = {k: runner.counters[k] - c0.get(k, 0) for k in runner.counters}
    print(
        f"   推进 {len(future)} 根({time.time() - t_before:.1f}s):"
        f"信号 {d['signals']} 成交 {d['fills']} 平仓段 {d['reduce_fills']}"
        f" 出 {d['exits']} 意图过期 {d['expired_intents']}"
        f" 容量跳 {d['cap_skips']} 名义跳 {d['min_notional_skips']}"
        f" 熔断拦 {d['breaker_blocks']} tg 消息 {len(tg2.messages)}"
    )
    assert runner.campaigns[sym].trig_bars[-1][0] == future[-1][0]

    print("== [3/5] 断点重启 round-trip(state.json 重载,不重开重单) ==")
    snap_positions = sorted(runner.positions)
    runner2 = build_runner(tmp, client, CollectTg(), cfg, contracts, contract_raw)
    assert runner2.campaigns[sym].trig_bars[-1][0] == warmup_end, "恢复窗越界"
    for bar in future:
        runner2.step(bar[0] + 1)
    assert sorted(runner2.positions) == snap_positions, (
        f"重启后持仓不一致:{sorted(runner2.positions)} != {snap_positions}"
    )
    new_oids = set(runner2.open_intents) - set(runner.open_intents)
    assert not new_oids, f"断点重启产生新意图(重单嫌疑):{sorted(new_oids)}"
    print(f"   持仓 {snap_positions} 恢复一致 ✔;无新增意图(断点幂等)✔(状态键 {len(runner2.open_intents)})")

    print("== [4/5] PAPER 对账消息样本 ==")
    for m in (
        "exchange_only BTCUSDT dir=-1 qty=0.002(异己,不动作)",
        "ours_only ETHUSDT dir=1 qty=0.05",
    ):
        print(f"   [tg 告警样本] 持仓对账不一致:{m}(异己仓不动作,人工核查)")
    bal = real.balance()
    print("   live 只读探测:account equity 存在 ✔(余额字段可达;paper 对账 N/A,此探测证私信链路)")
    assert bal["equity"] >= 0.0

    print("== [5/5] tg 连接测试(真实一条) ==")
    tgr = TelegramNotifier(secrets)
    ok = tgr.send_alert(
        "PAPER 端到端冒烟完成(market-operator 单链):30 根 bar 复放通过;tg 通道真实样例。"
    )
    print(f"   tg configured={tgr.configured} send_alert -> {ok}")
    assert ok, "tg 真实发送失败"
    print("PAPER 冒烟 PASS(零真单;全段断言通过)")


if __name__ == "__main__":
    main()
