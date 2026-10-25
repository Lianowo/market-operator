"""离线反事实核对(只读):用**已发生**的实盘日志+交易所成交,量化三处记账/风控
改动的方向与量级,不需要任何新的实盘时间。

三本账:
  1) 入场记账:参考价 E_ref vs 真实成交 E_true → 每笔的名义额/风险记账误差;
  2) 保护单被拒:stop_error 里"价格已穿越"的错误 → 出场价相对触发价的超额损失;
  3) 日界锚点:跨日缺口被旧锚抹掉的亏损,以及它额外给出的当日预算。

输出:var/tmp/CF.md / var/tmp/cf_validation.json
"""
from __future__ import annotations

import collections
import glob
import gzip
import json
import os
import re
import sys
from datetime import datetime, timezone

# tools/cf_validation.py → 仓库根是上上级(脚本原先放在 var/tmp/ 时是上三级)
# 依赖 var/tmp 下的当次中间产物(exchange_truth.json / rounds_stats.py),它们不入库;
# 结论已归档 reports/cf_validation_20261025.md。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "var", "tmp"))
from rounds_stats import build_rounds  # noqa: E402

TRUTH = os.path.join(ROOT, "var", "tmp", "exchange_truth.json")
LOG_DIR = os.path.join(ROOT, "logs")
OUT_MD = os.path.join(ROOT, "var", "tmp", "CF.md")
OUT_JS = os.path.join(ROOT, "var", "tmp", "cf_validation.json")
CROSSED = re.compile(r"止损价格请[<>]标记价格|止盈价格请[<>]标记价格", re.I)


def iso(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")


def day_of(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def read_journal() -> tuple[list[dict], list[dict]]:
    """返回 (entry fills, stop_error 行)。"""
    entries, errs = [], []
    paths = sorted(glob.glob(os.path.join(LOG_DIR, "live_*.jsonl"))) + \
        sorted(glob.glob(os.path.join(LOG_DIR, "live_*.jsonl.gz")))
    for p in paths:
        op = gzip.open if p.endswith(".gz") else open
        try:
            with op(p, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("kind") == "fill" and d.get("role") == "entry" \
                            and d.get("price") and d.get("qty"):
                        entries.append(d)
                    elif d.get("kind") == "stop_error":
                        errs.append(d)
        except OSError:
            continue
    return entries, errs


def pair_rounds(rounds, entries) -> list[dict]:
    used: set[int] = set()
    out = []
    for e in entries:
        best, best_dt = None, 10 ** 12
        for i, r in enumerate(rounds):
            if i in used or r["symbol"] != e["symbol"]:
                continue
            dt = abs(int(r["open_ts"]) - int(e["ts"]))
            if dt > 600_000:
                continue
            if abs(r["qty"] - float(e["qty"])) > max(0.02 * float(e["qty"]), 1e-9):
                continue
            if dt < best_dt:
                best, best_dt = i, dt
        if best is None:
            continue
        used.add(best)
        r = rounds[best]
        e_ref, e_true = float(e["price"]), float(r["entry"])
        out.append(dict(round=r, entry_ref=e_ref, entry_true=e_true,
                        slip_bps=(e_true - e_ref) / e_ref * 1e4
                        * (1 if r["long"] else -1),
                        dt_s=best_dt / 1000))
    return out


def desc(vals: list[float]) -> dict:
    if not vals:
        return dict(n=0, mean=0.0, med=0.0, p90=0.0, lo=0.0, hi=0.0)
    s = sorted(vals)
    n = len(s)
    return dict(n=n, mean=sum(s) / n, med=s[n // 2],
                p90=s[int(0.9 * (n - 1))], lo=s[0], hi=s[-1])


def main() -> int:
    data = json.loads(open(TRUTH, encoding="utf-8").read())
    rounds = build_rounds(data["fills"], data["bills"])
    entries, errs = read_journal()
    pairs = pair_rounds(rounds, entries)

    # ---- 账 1:入场记账误差 ----
    rows1 = []
    for p in pairs:
        r = p["round"]
        e_ref, e_true = p["entry_ref"], p["entry_true"]
        rows1.append(dict(symbol=r["symbol"], open=iso(r["open_ts"]),
                          long=r["long"], qty=r["qty"], ref=e_ref, tru=e_true,
                          slip_bps=p["slip_bps"], gross=r["gross"], net=r["net"],
                          notional_ref=r["qty"] * e_ref,
                          notional_true=r["qty"] * e_true,
                          dnotional=r["qty"] * (e_true - e_ref)))
    slip = desc([x["slip_bps"] for x in rows1])
    dnot = desc([x["dnotional"] for x in rows1])
    bias = desc([abs(x["dnotional"]) / max(abs(x["notional_ref"]), 1e-9) * 100
                 for x in rows1])

    # ---- 账 2:保护单被拒 —— 三类,处置完全不同 ----
    rows2 = []
    for e in errs:
        why = str(e.get("why") or "")
        if "43023" in why or "仓位不足" in why:
            kind = "no_pos"          # 交易所已无仓:本地残影
        elif CROSSED.search(why):
            kind = "crossed"         # 价格已穿越触发价:重试永不成功
        else:
            kind = "other"
        ts = int(e["ts"])
        mine = [r for r in rounds if r["symbol"] == e.get("symbol")]
        during = [r for r in mine if r["open_ts"] <= ts <= r["close_ts"]]
        after = sorted([r for r in mine if r["close_ts"] < ts],
                       key=lambda r: -r["close_ts"])
        r = during[0] if during else (after[0] if after else None)
        trig = float(e.get("trigger") or 0)
        extra = None
        if r is not None and trig:
            extra = (r["exit"] - trig) / trig * 1e4 * (-1 if r["long"] else 1)
        rows2.append(dict(symbol=e.get("symbol"), err=iso(ts), kind=kind,
                          alive=bool(during), trigger=trig,
                          exit=None if r is None else r["exit"],
                          exit_ts=None if r is None else iso(r["close_ts"]),
                          extra_bps=extra,
                          net=None if r is None else r["net"], why=why[:110]))
    by_kind = collections.Counter(x["kind"] for x in rows2)
    ex = desc([x["extra_bps"] for x in rows2
               if x["kind"] == "crossed" and x["alive"] and x["extra_bps"] is not None])

    # ---- 账 3:日界锚点缺口 ----
    bal = [(int(b["ts"]), float(b["bal"])) for b in data["bills"] if b.get("bal")]
    by_day: dict[str, list[tuple[int, float]]] = collections.defaultdict(list)
    for ts, v in bal:
        by_day[day_of(ts)].append((ts, v))
    days = sorted(by_day)
    rows3 = []
    for i in range(1, len(days)):
        prev_close = by_day[days[i - 1]][-1][1]
        first = by_day[days[i]][0][1]
        gap = first - prev_close
        rows3.append(dict(day=days[i], prev_close=prev_close, first=first,
                          gap=gap, old_budget=first * 0.10,
                          new_budget=prev_close * 0.10,
                          erased=max(0.0, -gap)))
    erased_tot = sum(x["erased"] for x in rows3)
    budget_tot = sum(x["new_budget"] - x["old_budget"] for x in rows3)

    L = ["# 离线反事实核对(基于已发生的实盘记录)", "",
         f"- 交易所成交 `{len(data['fills'])}` 笔,重建回合 `{len(rounds)}` 个;"
         f"journal 入场 fill `{len(entries)}` 条,成功配对 `{len(pairs)}` 笔",
         f"- 窗口 {iso(data['window'][0])} → {iso(data['window'][1])}", "",
         "## 账 1:入场记账(参考价 vs 真实成交)", "",
         f"- 不利偏移 bps:均值 {slip['mean']:+.2f} / 中位 {slip['med']:+.2f} / "
         f"p90 {slip['p90']:+.2f} / 最差 {slip['hi']:+.2f} / 最好 {slip['lo']:+.2f}"
         f"(n={slip['n']})",
         f"- 名义额记账误差 USDT:均值 {dnot['mean']:+.3f} / 中位 {dnot['med']:+.3f} / "
         f"最差 {dnot['lo']:+.3f} / 最好 {dnot['hi']:+.3f}",
         f"- 单笔名义额相对误差 %:均值 {bias['mean']:.2f} / 中位 {bias['med']:.2f} "
         f"/ p90 {bias['p90']:.2f}", "",
         "| 开仓 | symbol | 方向 | 数量 | 参考价 | 成交价 | 偏移bps | 名义差 | 净额 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for x in sorted(rows1, key=lambda y: -abs(y["slip_bps"]))[:15]:
        L.append(f"| {x['open']} | {x['symbol']} | {'L' if x['long'] else 'S'} | "
                 f"{x['qty']:.8g} | {x['ref']:.8g} | {x['tru']:.8g} | "
                 f"{x['slip_bps']:+.1f} | {x['dnotional']:+.3f} | {x['net']:+.3f} |")

    L += ["", "## 账 2:保护单被拒 —— 三类,处置完全不同", "",
          f"- `stop_error` 共 {len(errs)} 条;分类:"
          f"**交易所已无仓 {by_kind.get('no_pos', 0)}** / "
          f"**价格已穿越 {by_kind.get('crossed', 0)}** / 其它 {by_kind.get('other', 0)}",
          f"- 其中报错时仓位仍然存在(真正\"裸奔\")的:"
          f"{sum(1 for x in rows2 if x['alive'])} 条",
          f"- 该子集出场价相对触发价的超额(不利为正):均值 {ex['mean']:+.1f} bps / "
          f"中位 {ex['med']:+.1f} / 最差 {ex['hi']:+.1f}(n={ex['n']})", "",
          "| 报错时间 | symbol | 分类 | 报错时仍持仓 | 触发价 | 实际出场 | 超额bps | 该笔净额 | 错误原文 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for x in rows2:
        ex_s = "-" if x["exit"] is None else f"{x['exit']:.8g}"
        eb_s = "-" if x["extra_bps"] is None else f"{x['extra_bps']:+.1f}"
        nt_s = "-" if x["net"] is None else f"{x['net']:+.3f}"
        L.append(f"| {x['err']} | {x['symbol']} | {x['kind']} | "
                 f"{'是' if x['alive'] else '否'} | {x['trigger']:.8g} | "
                 f"{ex_s} | {eb_s} | {nt_s} | `{x['why']}` |")

    # ---- 账 3b:用**真实类**回放 10-25 跨日重启(交易所余额口径看不出这个缺口) ----
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import live_runner as lr  # noqa: E402

    def _ts(y, mo, d, h, mi):
        return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)

    prev_close, restart_eq = 107.6677, 100.3977   # 10-24 收盘 / 10-25 05:37 重启 tick
    t1, t2 = _ts(2026, 10, 24, 23, 58), _ts(2026, 10, 25, 5, 37)

    def trip(pre_fix: bool) -> tuple[float, float]:
        orig = lr._utc_day_gap
        if pre_fix:                     # 旧实现:锚 = 当日第一笔 tick
            lr._utc_day_gap = lambda *a, **k: None
        try:
            b = lr.DailyLossBreaker(0.10)
            b.update(t1, prev_close)
            b.update(t2, restart_eq)
            start = float(b.start_equity or 0.0)
            return start, start * 0.90
        finally:
            lr._utc_day_gap = orig

    old_start, old_trip = trip(True)
    new_start, new_trip = trip(False)
    lost = prev_close - restart_eq
    budget_left = prev_close * 0.10 - lost
    ratio = (old_start * 0.10) / budget_left if budget_left > 0 else float("inf")

    L += ["", "## 账 3:跨日锚点 —— 同一组事实喂给**真实 `DailyLossBreaker`**", "",
          "| 口径 | 当日锚 | 熔断线(-10%) | 说明 |", "|---|---|---|---|",
          f"| 修前(锚=当日首个 tick) | {old_start:.4f} | {old_trip:.4f} | "
          f"把 {lost:.4f} USDT({lost / prev_close:.2%})的缺口亏损抹掉 |",
          f"| 修后(锚=上一日最后一笔) | {new_start:.4f} | {new_trip:.4f} | "
          f"缺口计入当日预算 |",
          "",
          f"- 10-24 收盘 {prev_close:.4f} → 10-25 05:37 重启权益 {restart_eq:.4f},"
          f"缺口 **{lost:.4f} USDT**",
          f"- 修前当天可用亏损预算 `{old_start * 0.10:.4f}`,而**按纪律只剩** "
          f"`{budget_left:.4f}` ⇒ 多给 **{ratio:.2f}×** 的风险额度"]

    # (对照)交易所余额口径:停机期间交易所事件照常入账,所以看不出重启锚的问题
    L += ["", "### 账 3(对照):交易所余额口径 —— 为何复现不出上面的缺口", "",
          f"- 该口径下的\"跨日缺口\"合计 `{erased_tot:+.4f}` USDT、"
          f"预算差 `{budget_tot:+.4f}` USDT ≈ 0:"
          f"余额序列不含未实现盈亏,且停机期间的资金费/成交照常落账,"
          f"**日界权益被\"交易所事件\"连续化了**——所以这一口径不能用来判定熔断锚点。", "",
          "| 日期 | 前日收盘 | 当日首个余额 | 缺口 | 旧预算(10%) | 新预算(10%) |",
          "|---|---|---|---|---|---|"]

    for x in sorted(rows3, key=lambda y: y["gap"])[:10]:
        L.append(f"| {x['day']} | {x['prev_close']:.4f} | {x['first']:.4f} | "
                 f"{x['gap']:+.4f} | {x['old_budget']:.4f} | {x['new_budget']:.4f} |")

    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L) + "\n")
    json.dump(dict(entry=rows1, stop=rows2, day=rows3, slip=slip, dnot=dnot,
                   bias=bias, erased_tot=erased_tot, budget_tot=budget_tot),
              open(OUT_JS, "w", encoding="utf-8"), ensure_ascii=False,
              indent=1, default=str)
    print("\n".join(L))
    print(f"\n[out] {OUT_MD}\n[out] {OUT_JS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


