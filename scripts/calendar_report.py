"""列出非加密(RWA)合约实际使用的交易时段日历与来源(只读,不联网下单)。

为什么需要:代码里不写固定标的——`config/sessions.json` 只按命名族(货币对形状、
`HKD` 后缀、`TW` 前缀)解析,未登记标的走 `weekday_only` 兜底。运维用本命令看
每个合约落到哪个日历、来源是 override/pattern/default,再把需要精确时段的标的
写进 `data/calendars/overrides.json`(运行数据,不改代码、不改仓库配置)。

用法:
  python scripts/calendar_report.py                  # 从交易所公开接口取全部 RWA 合约
  python scripts/calendar_report.py --symbols AUSDT,BUSDT
  python scripts/calendar_report.py --unassigned     # 只列落回兜底的标的
  python scripts/calendar_report.py --json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

from market_operator import sessions
from market_operator.bitget_live import BitgetLiveClient


def _rwa_symbols(symbols: str | None) -> list[str]:
    if symbols:
        return sorted({s.strip().upper() for s in symbols.split(",") if s.strip()})
    rows = BitgetLiveClient(None).contracts()
    return sorted({str(r.get("symbol")) for r in rows
                   if str(r.get("isRwa") or "").upper() == "YES"})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None,
                    help="逗号分隔;缺省从交易所公开合约列表取全部 isRwa=YES")
    ap.add_argument("--unassigned", action="store_true",
                    help="只输出 source=default(尚未登记)的标的")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    syms = _rwa_symbols(args.symbols)
    rows = [sessions.describe(s) for s in syms]
    if args.unassigned:
        rows = [r for r in rows if r["source"] == "default"]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    counts = Counter((r["calendar"], r["source"]) for r in rows)
    print(f"[rwa] n={len(rows)}  calendar/source=" +
          ", ".join(f"{cal}:{src}={n}" for (cal, src), n in sorted(counts.items())))
    for r in rows:
        flag = "OPEN " if r["in_session"] else "CLOSED"
        print(f"  {flag} {r['symbol']:<16} {r['calendar']:<20} {r['source']}")
    if any(r["source"] == "default" for r in rows):
        print("[hint] source=default 的标的按'工作日可交易'兜底;"
              "需要精确时段请写入 data/calendars/overrides.json 的 assignments。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
