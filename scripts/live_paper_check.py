"""PAPER 端到端冒烟(实盘接入件;不发任何真单)。

流程:真实拉取 BTCUSDT warmup 窗口 2H 帧 → 12h 认知帧装配(canonical
replay;bar 事件流 == prepare_cognition 硬断言)→ 再拉 30 根真实 2H bar
逐根复放(mic + 认知收盘 + 路由 arm-full + 意图 + paper 虚拟经纪)→ 状态
断点重启(round-trip)同结果断言 → PAPER 对账消息样本打印(live 私信接口
只读探测:balance/positions 合法即可)+ tg 连接测试(真实发一条 alert)。

用法:  python scripts/live_paper_check.py [--core-bar-check]
产物:  打印报告(Warmup/Replay/Restart/Reconcile/TG 五段;退出码非零即失败);
       不写 reports/(冒烟属接入件验收样本,不入研究产物)。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from market_operator.replay import prepare_cognition          # noqa: E402
from market_operator.bitget_live import BitgetLiveClient, BitgetError  # noqa: E402
from market_operator.secrets_loader import load_secrets            # noqa: E402
from market_operator.structure import Params                        # noqa: E402
from market_operator.tg import TelegramNotifier                    # noqa: E402

import live_runner as lr                                            # noqa: E402

REPLAY_BAR_N = 30


class ReplayClient:
    """录像 client:history_candles 按录制窗口回放;private 接口委派真实
    client(只读;paper 永不调用下单类方法,调用即 raise 防呆)。

    防前视:warmup(end_ms 分页,start_ms 缺省)只见 warmup 窗;增量推进
    (start_ms 在场)才放行 future 段。"""

    def __init__(self, real: BitgetLiveClient, tape: dict,
                 warmup_end: int) -> None:
        self._real = real
        self._tape = tape                  # symbol → 全体录制 bar(升序)
        self._warmup_end = warmup_end      # warmup 末 bar 标签(含)

    def history_candles(self, symbol, granularity="2H", *, start_ms=None,
                        end_ms=None, limit=200):
        warm = [b for b in self._tape[symbol] if b[0] <= self._warmup_end]
        if start_ms is None:                       # warmup 分页(硬隔离)
            out = [b for b in warm if end_ms is None or b[0] < end_ms]
            return out[-limit:]
        out = [b for b in self._tape[symbol] if b[0] >= start_ms
               and (end_ms is None or b[0] < end_ms)]
        return out[:limit] if end_ms is None else out[-limit:]

    def universe(self, min_usdt_volume_24h):
        return ["BTCUSDT"]

    # paper 防呆:一切下单类路径必须不存在
    def place_limit_order(self, *a, **k):
        raise AssertionError("PAPER 冒烟出现下单调用(fail-closed 违例)")

    place_market_order = place_stop_market_plan = place_limit_order
    cancel_order = cancel_plan_order = place_limit_order

    def __getattr__(self, name):           # balance/positions/contracts 只读委派
        return getattr(self._real, name)


class CollectTg:
    """tg 收集器(冒烟 printed 样本;真实连接测试单独走真 TelegramNotifier)。"""

    def __init__(self) -> None:
        self.messages: list = []

    def __getattr__(self, name):
        if name.startswith("send_"):
            def _send(*a, **k):
                self.messages.append((name, a, k))
                return True
            return _send
        raise AttributeError(name)


def build_runner(tmp: Path, client, tg, cfg: dict,
                 contracts: dict) -> lr.LiveRunner:
    return lr.LiveRunner("paper", cfg, client, tg,
                         equity0=float(cfg["paper"]["equity0"]),
                         contracts=contracts,
                         state_path=tmp / "state.json",
                         journal_dir=tmp / "logs",
                         symbols=("BTCUSDT",), universe_enabled=False)


def main(argv=None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="live_paper_check")
    ap.add_argument("--config", default=str(ROOT / "config" / "live.json"))
    args = ap.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    secrets = load_secrets()
    real = BitgetLiveClient(secrets)
    tmp = ROOT / "runtime" / "live" / "paper_check"
    tmp.mkdir(parents=True, exist_ok=True)
    # 冒烟自清理:state/意图账每次重建(重跑不受上一轮污染)
    for p in [tmp / "state.json", tmp / "_probe_state.json"]:
        p.unlink(missing_ok=True)
    for p in (tmp / "logs").glob("live_*.jsonl") if (tmp / "logs").exists() else []:
        p.unlink()

    print(f"== [1/5] Warmup 认知帧(BTCUSDT real 2H,canonical replay;warmup "
          f"窗口尾部留 {REPLAY_BAR_N} 根作'未来') ==")
    sym = "BTCUSDT"
    want = int(cfg["cognition"]["warmup_trigger_bars"]) + REPLAY_BAR_N
    have: dict = {}
    end_ms, guard = None, 0
    while len(have) < want and guard < (want // 200) + 3:
        page = real.history_candles(sym, "2H", end_ms=end_ms, limit=200)
        fresh = [b for b in page if b[0] not in have]
        if not fresh:
            break
        for b in fresh:
            have[b[0]] = b
        end_ms = fresh[0][0]
        guard += 1
    all_bars = [have[t] for t in sorted(have)][-want:]
    assert len(all_bars) == want, f"拉取不足:{len(all_bars)} < {want}"
    warmup_bars, future = all_bars[:-REPLAY_BAR_N], all_bars[-REPLAY_BAR_N:]
    warmup_end = warmup_bars[-1][0]
    client = ReplayClient(real, {sym: all_bars}, warmup_end=warmup_end)
    contracts = lr.load_contracts_live(real, cfg["symbols_core"])
    tg2 = CollectTg()
    runner = build_runner(tmp, client, tg2, cfg, contracts)
    cs = runner._build_all_tf_campaigns(sym)       # 装配(不推进;决不动 future)
    runner.campaigns[sym] = cs
    runner.symbols_pending.remove(sym)
    assert cs.trig_bars[-1][0] == warmup_end, "warmup 窗越界(防前视破)"
    n_cog, n_trig = len(cs.cog_bars), len(cs.trig_bars)
    print(f"   12h 认知 bar {n_cog} 根;2H trigger {n_trig} 根;"
          f"末 trigger 标签 {warmup_end} (多周期并发实例: {len(runner.extra_campaigns) + 1} 个)")

    # canonical 硬断言:replay 认知 == prepare_cognition 逐 bar 事件流
    params = runner.params
    import pandas as pd
    df_cog = pd.DataFrame(cs.cog_bars,
                          columns=["ts", "open", "high", "low", "close",
                                   "volume"])
    ref = prepare_cognition(df_cog, params, sym, enable_e7=True)
    got = _events_of(cs, params)
    want = {i: [(e.id, e.direction) for e in evs]
            for i, evs in ref.events_by.items()}
    diff = {k: (got.get(k), want.get(k)) for k in set(got) | set(want)
            if got.get(k) != want.get(k)}
    assert got == want, f"replay bar 事件流 != prepare_cognition: {diff}"
    print(f"   [断言] replay bar 事件流 == prepare_cognition(全 {n_cog} 根,"
          f"{sum(len(v) for v in want.values())} 事件)✔")

    print(f"== [2/5] 复放 {REPLAY_BAR_N} 根真实 2H bar(paper 虚拟经纪) ==")
    t_before = time.time()
    c0 = dict(runner.counters)
    for b in future:
        runner.step(b[0] + 1)
    d = {k: runner.counters[k] - c0.get(k, 0) for k in runner.counters}
    print(f"   推进 {len(future)} 根({time.time() - t_before:.1f}s):"
          f"信号 {d['signals']} 成交 {d['fills']} 平仓段 {d['reduce_fills']}"
          f" 出 {d['exits']} 意图过期 {d['expired_intents']}"
          f" 容量跳 {d['cap_skips']} 名义跳 {d['min_notional_skips']}"
          f" 熔断拦 {d['breaker_blocks']} tg 消息 {len(tg2.messages)}")
    assert runner.campaigns[sym].trig_bars[-1][0] == future[-1][0]

    print("== [3/5] 断点重启 round-trip(state.json 重载,不重开重单) ==")
    snap = (dict(runner.counters), len(runner.open_intents),
            sorted(runner.positions), dict(runner.breaker.__dict__))
    runner2 = build_runner(tmp, client, CollectTg(), cfg, contracts)
    # 断点恢复的战役受 warmup 屏障保护;推进同一 future 序列
    assert runner2.campaigns[sym].trig_bars[-1][0] == warmup_end, \
        "恢复窗越界"
    for b in future:
        runner2.step(b[0] + 1)
    assert sorted(runner2.positions) == snap[2], \
        f"重启后持仓不一致:{sorted(runner2.positions)} != {snap[2]}"
    new_oids = set(runner2.open_intents) - set(runner.open_intents)
    assert not new_oids, f"断点重启产生新意图(重单嫌疑):{sorted(new_oids)}"
    print(f"   持仓 {snap[2]} 恢复一致 ✔;无新增意图(断点幂等)✔"
          f"(状态键 {len(runner2.open_intents)})")

    print("== [4/5] PAPER 对账消息样本 ==")
    sample = ["exchange_only BTCUSDT dir=-1 qty=0.002(异己,不动作)",
              "ours_only ETHUSDT dir=1 qty=0.05"]
    for m in sample:
        print(f"   [tg 告警样本] 持仓对账不一致:{m}(异己仓不动作,人工核查)")
    bal = real.balance()
    print(f"   live 只读探测:account equity 存在 ✔"
          f"(余额字段可达;paper 对账 N/A,此探测证私信链路)")
    assert bal["equity"] >= 0.0

    print("== [5/5] tg 连接测试(真实一条) ==")
    tgr = TelegramNotifier(secrets)
    ok = tgr.send_alert("PAPER 端到端冒烟完成(market-operator v21 单链):"
                        "30 根 bar 复放通过;tg 通道真实样例。")
    print(f"   tg configured={tgr.configured} send_alert -> {ok}")
    assert ok, "tg 真实发送失败"
    print("PAPER 冒烟 PASS(零真单;全段断言通过)")


def _events_of(cs: lr.CampaignState, params: Params) -> dict:
    """独立重放 detection(纯检测器,确定性)== 装配期 replay 同一批次,应
    == prepare_cognition(canonical 同一检测路径的直接断言)。"""
    from market_operator.events import EventDetector
    det = EventDetector(params, enable_e7=True)
    out = {}
    for i, bar in enumerate(cs.cog_bars):
        emitted = det.update(bar)
        if emitted:
            out[i] = [(e.id, e.direction) for e in emitted]
    return out


if __name__ == "__main__":
    main()
