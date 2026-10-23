"""缺口补做**预演**:用真实行情 + 真实交易所持仓,把补做要下的单全部拦在本地。

为什么需要:重启时补做会真的发市价单(止损追平 / TP1 减仓)。上线前必须能
先看清"这一次重启会做什么"。本脚本用只读 client 包一层写拦截 —— 下单/撤单/
改杠杆一律记账不发送,其余(持仓/权益/挂单/行情)全部走真实接口。

用法:  python scripts/live_gap_preview.py [--config config/live.json]
产物:  仅打印(不改 state.json、不下单);退出码 0。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from market_operator.bitget_live import BitgetLiveClient, BitgetError   # noqa: E402
from market_operator.secrets_loader import load_secrets                 # noqa: E402

import live_runner as lr                                                # noqa: E402

CONFIG_PATH = ROOT / "config" / "live.json"


class BlockedClient:
    """读走真接口;写全部拦截并记账(返回假 orderId 让流程走完)。"""

    WRITES = ("place_market_order", "place_limit_order",
              "place_stop_market_plan", "cancel_order", "cancel_plan_order",
              "set_leverage")

    def __init__(self, real):
        self._real = real
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        if name in self.WRITES:
            def _blocked(*a, **kw):
                self.calls.append((name, a, kw))
                return {"orderId": f"PREVIEW{len(self.calls)}"}
            return _blocked
        return getattr(self._real, name)


class _TG:
    def __init__(self):
        self.alerts: list[str] = []
        self.fills: list[dict] = []

    def send_alert(self, msg, **_kw):
        self.alerts.append(str(msg))

    def send_fill(self, sym, title, qty, price, **kw):
        self.fills.append({"symbol": sym, "title": title, "qty": qty,
                           "price": price, **kw})


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="live_gap_preview")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    args = ap.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    state_path = ROOT / str((cfg.get("loop") or {})
                            .get("state_path", "runtime/live/state.json"))
    cred_src = cfg.get("credentials_source")
    cred_path = ((ROOT / cred_src)
                 if cred_src and (ROOT / cred_src).exists() else None)
    secrets = load_secrets(cred_path, config_overrides=cfg.get("credentials"))
    real = BitgetLiveClient(secrets)
    client = BlockedClient(real)
    contracts, raw = lr.load_contracts_live(real)

    # 状态副本:补做过程中 runner 只改内存与本副本,真实 state.json 不动
    tmp = Path(tempfile.mkdtemp(prefix="gap_preview_"))
    work = tmp / "state.json"
    if state_path.exists():
        shutil.copy2(state_path, work)
    # 冷档(campaigns.json)必须一起复制:缺口批次是从它的尾部 bar 切出来的,
    # 单独复制热档会让预演"看不到任何缺口",给出虚假的安心。
    cold = state_path.with_name("campaigns.json")
    if cold.exists():
        shutil.copy2(cold, tmp / "campaigns.json")
    candles_src = state_path.parent / "candles"
    if candles_src.exists():
        shutil.copytree(candles_src, tmp / "candles")
    tg = _TG()
    r = lr.LiveRunner("live", cfg, client, tg, state_path=work,
                      journal_dir=tmp / "logs", contracts=contracts,
                      contract_raw=raw, symbols=tuple(cfg["symbols_core"]))
    now_ms = int(time.time() * 1000)
    gap_s = 0.0 if r._gap_started_ms is None else \
        max(0.0, (now_ms - r._gap_started_ms) / 1000.0)

    print("=" * 78)
    print(f"预演起点 saved_at_utc / 缺口 {gap_s / 3600:.2f}h")
    print(f"缺口批次:{len(r._gap_bars)} 品种 / "
          f"{sum(len(v) for v in r._gap_bars.values())} 根 bar")
    print(f"本地持仓 {len(r.positions)} 个: {sorted(r.positions)}")
    print("=" * 78)
    before = {s: dict(p) for s, p in r.positions.items()}
    ok = r.recover_gap(now_ms)
    print(f"recover_gap → {ok}")

    print()
    print("将要发出的单(全部被拦截):")
    if not client.calls:
        print("  (无)")
    for name, a, kw in client.calls:
        detail = {k: kw[k] for k in ("side", "size", "trade_side",
                                     "trigger_price", "plan_type",
                                     "client_oid") if k in kw}
        print(f"  {name}{list(a)}{detail}")
    print()
    print("成交/离场(本地记账,仅预演):")
    for f in tg.fills:
        print(f"  {f['symbol']:<12}{f['title']:<16}qty={f['qty']:.8g} "
              f"px={f['price']:.8g} pnl={f.get('pnl')}")
    print()
    print("告警:")
    for a in tg.alerts:
        print(f"  {a}")
    print()
    print("持仓变化:")
    for sym in sorted(set(before) | set(r.positions)):
        b = before.get(sym)
        a = r.positions.get(sym)
        if b is None:
            print(f"  {sym:<12} + 新仓 {a}")
        elif a is None:
            print(f"  {sym:<12} - 离场(原 qty={b['qty']})")
        elif abs(float(b["qty"]) - float(a["qty"])) > 1e-12:
            print(f"  {sym:<12} ~ 减仓 {b['qty']} → {a['qty']}")
    print()
    print(f"counters: {r.counters}")
    print(f"(预演目录 {tmp};可安全删除)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
