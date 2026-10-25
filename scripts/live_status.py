"""实盘只读体检(不下单、不撤单、不改杠杆、不写 state.json)。

回答三个运维问题:
① 心跳是否新鲜(state.json 的 saved_at_utc 年龄);
② 仓位与保护是否一致(本地 positions/stops ↔ 交易所持仓 ↔ 在挂 TPSL/计划单);
③ 重启会发生什么(缺口段已收盘 2H bar 是否已越过当前防线,即补做是否会
   触发市价离场)。

用法:  python scripts/live_status.py [--config config/live.json] [--gap-bars N]
产物:  仅打印(退出码 0;读不到 state/交易所时非零,便于外部监控判活)。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from market_operator import paths                                # noqa: E402
from market_operator.bitget_live import BitgetLiveClient, BitgetError   # noqa: E402
from market_operator.secrets_loader import load_secrets                 # noqa: E402

import live_runner as lr                                                # noqa: E402

CONFIG_PATH = ROOT / "config" / "live.json"


def _fmt_ts(ms) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) \
        .strftime("%Y-%m-%d %H:%M UTC")


def _age_h(ms, now_ms: int) -> str:
    return "?" if not ms else f"{(now_ms - int(ms)) / 3_600_000.0:.2f}h"


def _print_state(state_path: Path, now_ms: int) -> tuple[dict, int]:
    print("=" * 78)
    print("state.json")
    print("=" * 78)
    if not state_path.exists():
        print(f"缺失: {state_path}")
        return {}, 2
    st = json.loads(state_path.read_text(encoding="utf-8"))
    saved_ms = lr._utc_ms(st.get("saved_at_utc"))
    print(f"path        : {state_path}")
    print(f"saved_at    : {st.get('saved_at_utc')}  "
          f"(age {_age_h(saved_ms, now_ms)})")
    print(f"equity      : {st.get('equity')}")
    cold = state_path.with_name("campaigns.json")
    n_camps = len(st.get("campaigns") or {})
    if cold.exists():
        cold_ms = int(cold.stat().st_mtime * 1000)
        if not n_camps:
            try:
                n_camps = len(json.loads(
                    cold.read_text(encoding="utf-8")).get("campaigns") or {})
            except ValueError:
                n_camps = -1
        print(f"campaigns   : {cold.name} {cold.stat().st_size / 1024:.0f}KB "
              f"(mtime age {_age_h(cold_ms, now_ms)})")
    else:
        print("campaigns   : 缺失(campaigns.json;旧格式内嵌在 state.json)")
    print(f"positions   : {len(st.get('positions') or {})}  "
          f"stops={len(st.get('stops') or {})}  "
          f"campaigns={n_camps}")
    print(f"breaker     : {st.get('breaker')}")
    print(f"gap anchor  : {st.get('gap_started_ms')} "
          f"(未补完缺口品种={len(st.get('gap_bars') or {})})")
    ctr = st.get("counters") or {}
    keep = {k: ctr[k] for k in
            ("orders_placed", "order_errors", "stop_errors", "stop_missing",
             "stop_dupes", "rate_skips", "gap_recoveries", "gap_stop_exits",
             "gap_reduce_skips", "exits") if k in ctr}
    print(f"counters    : {keep}")
    return st, 0


def _read_exchange(cfg: dict):
    print()
    print("=" * 78)
    print("交易所(live 只读)")
    print("=" * 78)
    try:
        cred_src = cfg.get("credentials_source")
        cred_path = paths.resolve_existing(cred_src) if cred_src else None
        if cred_path is not None and not cred_path.exists():
            cred_path = None
        secrets = load_secrets(cred_path,
                               config_overrides=cfg.get("credentials"))
        client = BitgetLiveClient(secrets)
        bal = client.balance()
        print(f"equity={bal['equity']:.4f} available={bal['available']:.4f} "
              f"unrealized={bal['unrealized_pl']:.4f}")
        ex_pos = {p["symbol"]: p for p in client.positions()}
        raw = client.tpsl_orders_pending() or []
        rows = raw.get("entrustedList") if isinstance(raw, dict) else raw
        pending: dict = {}
        for r in rows or []:
            pending.setdefault(str(r.get("symbol")), []).append(r)
        print(f"持仓 {len(ex_pos)} 个;在挂 TPSL/计划单 {len(rows or [])} 张"
              f"({len(pending)} 个品种)")
        return client, ex_pos, pending, 0
    except (BitgetError, RuntimeError, OSError) as e:
        print(f"交易所读取失败:{type(e).__name__} {str(e)[:160]}")
        return None, {}, {}, 3



def _print_positions(positions: dict, stops: dict, ex_pos: dict,
                     pending: dict, has_client: bool) -> list[str]:
    print()
    print("=" * 78)
    print("逐仓核对 (本地 ↔ 交易所)")
    print("=" * 78)
    head = (f"{'symbol':<12}{'dir':>4}{'qty':>12}{'entry':>12}{'mark':>12}"
            f"{'stop':>12}{'dist%':>8}  {'stop单':<8} 状态")
    print(head)
    print("-" * 96)
    issues: list[str] = []
    for sym in sorted(set(positions) | set(ex_pos)):
        loc, ex = positions.get(sym), ex_pos.get(sym)
        if loc and not ex:
            print(f"{sym:<12}{'':>44} 本地有仓/交易所已无 → 重启后清理")
            issues.append(f"{sym}: 本地残留仓位(交易所已无)")
            continue
        if ex and not loc:
            print(f"{sym:<12}{ex['direction']:>4}{ex['qty']:>12.8g}"
                  f"{ex['entry_price']:>12.8g}{ex['mark_price']:>12.8g}"
                  f"{'-':>12}{'-':>8}  {'':<8} 交易所独有")
            issues.append(f"{sym}: 交易所仓位未被本地接管")
            continue
        d = int(loc["direction"])
        stop = float(loc["invalid"])
        mark = float(ex["mark_price"] or loc.get("mark") or 0.0)
        dist = (mark - stop) / mark * 100 if mark else 0.0
        stop_oid = str((stops.get(sym) or {}).get("order_id") or "")
        live_ids = {str(r.get("orderId")) for r in pending.get(sym, [])}
        state = "OK"
        if not stop_oid:
            stop_txt, state = "无记录", "无保护止损"
            issues.append(f"{sym}: 本地无止损记录")
        elif has_client and stop_oid not in live_ids:
            stop_txt, state = "已不在挂", "保护失效"
            issues.append(f"{sym}: 止损单不在交易所")
        elif len(pending.get(sym, [])) > 1:
            stop_txt = f"x{len(pending.get(sym, []))}"
            state = "重复止损单"
            issues.append(f"{sym}: 同品种多张保护单")
        else:
            stop_txt = "在挂"
        if (mark <= stop) if d > 0 else (mark >= stop):
            state = "已越防线"
            issues.append(f"{sym}: 现价已越过防线")
        if abs(float(loc["qty"]) - float(ex["qty"])) > \
                max(1e-9, float(ex["qty"]) * 1e-6):
            state += f" qty差{float(loc['qty']) - float(ex['qty']):+.8g}"
            issues.append(f"{sym}: 数量与交易所不一致")
        print(f"{sym:<12}{d:>4}{float(loc['qty']):>12.8g}"
              f"{float(loc['entry']):>12.8g}{mark:>12.8g}{stop:>12.8g}"
              f"{dist:>8.2f}  {stop_txt:<8} {state}")
    return issues



def _print_gap(client, cfg: dict, st: dict, positions: dict, now_ms: int,
               show_bars: int) -> None:
    print()
    print("=" * 78)
    print("缺口补做预演(只读:按 state 里的防线判定,不模拟补放后的收紧)")
    print("注意:防线是**当前**值。补做后防线会被收紧,历史 bar 越过当前防线")
    print("      不代表当时真挂着那张单 —— 以日志 gap_stop_hit 为准。")
    print("=" * 78)
    loop_cfg = cfg.get("loop") or {}
    anchor = st.get("gap_started_ms") or lr._utc_ms(st.get("saved_at_utc"))
    if not anchor:
        print("无缺口锚点(state 缺 saved_at_utc/gap_started_ms)")
        return
    gap_s = max(0.0, (now_ms - int(anchor)) / 1000.0)
    min_gap = float((loop_cfg.get("gap_recovery") or {})
                    .get("min_gap_seconds", 180))
    print(f"缺口起点 : {_fmt_ts(anchor)}  (长度 {gap_s / 3600:.2f}h, 阈值 "
          f"{min_gap / 60:.0f}min → "
          f"{'会触发补做' if gap_s >= min_gap else '不触发补做'})")
    if client is None:
        return
    gran = str((cfg.get("cognition") or {}).get("granularity") or "2H")
    for sym in sorted(positions):
        loc = positions[sym]
        d, stop = int(loc["direction"]), float(loc["invalid"])
        try:
            bars = client.history_candles(sym, gran, start_ms=int(anchor),
                                          limit=100)
        except BitgetError as e:
            print(f"  {sym:<12} 取 bar 失败:{str(e)[:80]}")
            continue
        if not bars:
            print(f"  {sym:<12} 缺口内无已收盘 bar")
            continue
        hit = next((b for b in bars
                    if (float(b[3]) <= stop if d > 0
                        else float(b[2]) >= stop)), None)
        ext = min(float(b[3]) for b in bars) if d > 0 else \
            max(float(b[2]) for b in bars)
        verdict = (f"当前防线曾被越过({_fmt_ts(hit[0])};非\"当时挂着\")"
                   if hit else "未触及")
        print(f"  {sym:<12} bars={len(bars):>2} extreme={ext:<12.8g}"
              f"stop={stop:<12.8g} {verdict}")
        for b in (bars[-show_bars:] if show_bars else []):
            print(f"      {_fmt_ts(b[0])} o={b[1]:.8g} h={b[2]:.8g} "
                  f"l={b[3]:.8g} c={b[4]:.8g}")


def _runner_pids() -> list[str]:
    """实际列出在跑的 live_runner 进程(修前这里只打印了命令字符串)。"""
    if os.name == "nt":
        cmd = ["powershell", "-NoProfile", "-Command",
               "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\""
               " | Where-Object { $_.CommandLine -match 'live_runner' }"
               " | ForEach-Object { \"{0} ({1})\" -f $_.ProcessId, $_.Name }"]
    else:
        cmd = ["pgrep", "-af", "live_runner"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return [f"查询失败: {e}"]
    return [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="live_status")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--gap-bars", type=int, default=0,
                    help="每品种额外打印的缺口 bar 数(默认只给结论)")
    args = ap.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    state_path = paths.resolve((cfg.get("loop") or {})
                               .get("state_path", "runtime/live/state.json"))
    now_ms = int(time.time() * 1000)

    st, rc = _print_state(state_path, now_ms)
    positions, stops = st.get("positions") or {}, st.get("stops") or {}
    client, ex_pos, pending, rc_ex = _read_exchange(cfg)
    issues = _print_positions(positions, stops, ex_pos, pending,
                              client is not None)
    _print_gap(client, cfg, st, positions, now_ms, args.gap_bars)

    print()
    print("=" * 78)
    saved_ms = lr._utc_ms(st.get("saved_at_utc")) if st else None
    stale = bool(saved_ms and now_ms - saved_ms > 180_000)
    print(f"心跳年龄 : {_age_h(saved_ms, now_ms)}"
          f"{' → 超过 3min,进程疑似停摆' if stale else ''}")
    pids = _runner_pids()
    print("进程列表 : " + (", ".join(pids) if pids
                           else "未发现运行中的 live_runner(检查计划任务)"))
    print()
    if issues:
        print("可疑项:")
        for it in issues:
            print(f"  ! {it}")
    else:
        print("结论:本地 / 交易所 / 保护单三者一致,无越界。")
    # 可疑项必须体现在退出码上,否则脚本化巡检(计划任务/cron)拿不到信号:
    # 修前只有"读不到交易所"才非 0,仓位/止损错配一律 return 0。
    return 1 if issues else (rc or rc_ex)


if __name__ == "__main__":
    raise SystemExit(main())

