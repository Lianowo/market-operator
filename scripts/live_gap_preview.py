"""缺口补做**预演**:用真实行情 + 真实交易所持仓,把补做要下的单全部拦在本地。

为什么需要:重启时补做会真的发市价单(止损追平 / TP1 减仓)。上线前必须能
先看清"这一次重启会做什么"。本脚本用只读 client 包一层写拦截 —— 下单/撤单/
改杠杆一律记账不发送,其余(持仓/权益/挂单/行情)全部走真实接口。

用法:  python scripts/live_gap_preview.py [--config config/live.json]
产物:  仅打印(不改 state.json、不下单);退出码 0。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from market_operator import live_support, ops, paths                   # noqa: E402
from market_operator.bitget_live import BitgetLiveClient             # noqa: E402

import live_runner as lr                                             # noqa: E402

CONFIG_PATH = ROOT / "config" / "live.json"


class BlockedClient:
    """Strict read-only proxy: only allowlisted queries hit the real exchange.

    Every non-read-only method (including newly added order/TPSL/leverage write
    endpoints) is intercepted locally so a preview run can never transmit an
    order or mutate exchange state.
    """

    READ_ONLY: frozenset[str] = frozenset({
        "tickers",
        "universe",
        "history_candles",
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

    def __init__(self, real: BitgetLiveClient) -> None:
        self._real = real
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if name in self.READ_ONLY:
            return getattr(self._real, name)

        def _blocked(*args: Any, **kwargs: Any) -> dict[str, str]:
            self.calls.append((name, args, kwargs))
            return {"orderId": f"PREVIEW{len(self.calls)}"}

        return _blocked


class _PreviewTG:
    def __init__(self) -> None:
        self.alerts: list[str] = []
        self.fills: list[dict[str, Any]] = []

    def send_alert(self, msg: str, **_kw: Any) -> bool:
        self.alerts.append(str(msg))
        return True

    def send_fill(
        self,
        sym: str,
        title: str,
        qty: float,
        price: float,
        **kw: Any,
    ) -> bool:
        self.fills.append({"symbol": sym, "title": title, "qty": qty, "price": price, **kw})
        return True


def _stage_preview_state(state_path: Path) -> tuple[Path, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="gap_preview_"))
    work = tmp / "state.json"
    if state_path.exists():
        shutil.copy2(state_path, work)
    cold = state_path.with_name("campaigns.json")
    if cold.exists():
        shutil.copy2(cold, tmp / "campaigns.json")
    candles_src = state_path.parent / "candles"
    if candles_src.exists():
        shutil.copytree(candles_src, tmp / "candles")
    return tmp, work


def main(argv: Sequence[str] | None = None) -> int:
    paths.soften_stdio()
    ap = argparse.ArgumentParser(prog="live_gap_preview")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    args = ap.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    state_path = paths.resolve(
        (cfg.get("loop") or {}).get("state_path", "runtime/live/state.json")
    )
    secrets = ops.load_secrets(cfg)
    real = BitgetLiveClient(secrets)
    client = BlockedClient(real)
    contracts, raw = live_support.load_contracts_live(real)

    tmp, work = _stage_preview_state(state_path)
    tg = _PreviewTG()
    runner = lr.LiveRunner(
        "live",
        cfg,
        client,
        tg,  # type: ignore[arg-type]
        state_path=work,
        journal_dir=tmp / "logs",
        contracts=contracts,
        contract_raw=raw,
        symbols=tuple(cfg.get("symbols_core") or ()),
    )
    now_ms = int(time.time() * 1000)
    gap_s = (
        0.0
        if runner._gap_started_ms is None
        else max(0.0, (now_ms - runner._gap_started_ms) / 1000.0)
    )

    print("=" * 78)
    print(f"预演起点 saved_at_utc / 缺口 {gap_s / 3600:.2f}h")
    print(
        f"缺口批次:{len(runner._gap_bars)} 品种 / "
        f"{sum(len(v) for v in runner._gap_bars.values())} 根 bar"
    )
    print(f"本地持仓 {len(runner.positions)} 个: {sorted(runner.positions)}")
    print("=" * 78)
    before = {s: dict(p) for s, p in runner.positions.items()}
    ok = runner.recover_gap(now_ms)
    print(f"recover_gap → {ok}")

    print("\n将要发出的单(全部被拦截):")
    if not client.calls:
        print("  (无)")
    for name, call_args, kw in client.calls:
        detail = {
            k: kw[k]
            for k in ("side", "size", "trade_side", "trigger_price", "plan_type", "client_oid")
            if k in kw
        }
        print(f"  {name}{list(call_args)}{detail}")

    print("\n成交/离场(本地记账,仅预演):")
    for fill in tg.fills:
        print(
            f"  {fill['symbol']:<12}{fill['title']:<16}qty={fill['qty']:.8g} "
            f"px={fill['price']:.8g} pnl={fill.get('pnl')}"
        )

    print("\n告警:")
    for alert in tg.alerts:
        print(f"  {alert}")

    print("\n持仓变化:")
    for sym in sorted(set(before) | set(runner.positions)):
        prev_pos = before.get(sym)
        cur_pos = runner.positions.get(sym)
        if prev_pos is None:
            print(f"  {sym:<12} + 新仓 {cur_pos}")
        elif cur_pos is None:
            print(f"  {sym:<12} - 离场(原 qty={prev_pos['qty']})")
        elif abs(float(prev_pos["qty"]) - float(cur_pos["qty"])) > 1e-12:
            print(f"  {sym:<12} ~ 减仓 {prev_pos['qty']} → {cur_pos['qty']}")

    print(f"\ncounters: {runner.counters}")
    print(f"(预演目录 {tmp};可安全删除)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
