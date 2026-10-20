"""守护计划观察哨:每小时真实数据重放,宣判每个计划的状态并可审计。

状态机(每次运行从创建时刻起确定性重算):
  WATCHING(价格未进入入场区)
  IN_ZONE(首次进入)
  TRIGGERED(E2式扫进收回 / E3拒绝 成立 → 次开盘=模拟入场价)
  POSITION(模拟持仓: 止损/目标/超时 逐 bar 解决, maker→taker 全taker简化, 费用同 canonical)
  CLOSED(出局/作废), DEAD(推翻条件)

输出: 终端报告 + 追加 reports/plan_watch_log.jsonl。
"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, "src")
from market_operator import data
from market_operator.structure import Params
from market_operator.events import EventDetector

PARAMS_PATH = "config/canonical.json"
PLANS_PATH = "config/plan_watch.json"
LOG_PATH = Path("reports/plan_watch_log.jsonl")


def frame(symbol):
    df = data.load(symbol)
    fresh = data.fetch_klines(symbol, granularity="1H", years=0.01)
    return pd.concat([df, fresh]).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def replay_plan(plan, df, det_events):
    p = Params.from_json(PARAMS_PATH)
    created = pd.Timestamp(plan["created_ts"]).value // 10**6
    z_lo, z_hi = plan["entry_zone"]
    start = df[df.ts >= created]
    if start.empty:
        return {"state": "WATCHING", "detail": "无创建后数据"}
    idx0 = start.index[0]
    side = plan["side"]
    sgn = 1 if side == "long" else -1
    entry_i = None; trig_kind = None
    # 计划区对应的 E3/E2 事件(创建后)
    cand = [e for e in det_events if e.known_index >= idx0 and e.id in ("E3", "E2")
            and e.direction == sgn and
            e.attrs.get("zone") and e.attrs["zone"].hi >= z_lo and e.attrs["zone"].lo <= z_hi]
    for i, b in enumerate(df.iloc[idx0:].itertuples()):
        gi = idx0 + i
        if sgn == -1 and b.close > plan["dead_on_close_above"]:
            return {"state": "DEAD", "reason": f"收盘>{plan['dead_on_close_above']} 推翻", "ts": int(b.ts)}
        in_zone = (b.high >= z_lo and b.low <= z_hi) if True else False
        if in_zone and entry_i is None:
            hit = [e for e in cand if e.known_index == gi]
            if hit:
                entry_i = gi + 1  # 次开盘成交
                trig_kind = hit[0].id
                break
    if entry_i is None or entry_i >= len(df):
        last = df.iloc[-1]
        where = "inside" if (last.low <= z_hi and last.high >= z_lo) else ("below" if last.close < z_lo else "above")
        return {"state": "WATCHING", "zone_pos": where,
                "last_close": float(last.close),
                "triggers_armed": f"{trig_kind or '无'}",
                "need": "价格进入[%s,%s]且出现 E2/E3 确认" % (z_lo, z_hi)}
    ent = float(df.open.iloc[entry_i])
    risk = plan["risk_frac"] * plan["equity_usdt"]
    per_r = abs(ent - plan["invalid"])
    qty = risk / per_r
    pos = qty; pnl = 0.0; tgt_hit = 0
    for j in range(entry_i, len(df)):
        b = df.iloc[j]
        stop = plan["invalid"]
        out = None
        if (sgn == -1 and b.close > stop) or (sgn == 1 and b.close < stop):
            out_price = float(df.open.iloc[j + 1]) if j + 1 < len(df) else float(b.close)
            move = (ent - out_price) if sgn == -1 else (out_price - ent)
            pnl += move * pos
            return {"state": "CLOSED", "exit": "止损", "r": round(pnl / risk, 3),
                    "pnl_usdt": round(pnl, 3), "entry_i": entry_i, "trig": trig_kind}
        if tgt_hit < len(plan["targets"]):
            tp = plan["targets"][tgt_hit]
            reached = (b.low <= tp) if sgn == -1 else (b.high >= tp)
            if reached:
                move = (ent - tp) if sgn == -1 else (tp - ent)
                pnl += move * pos * 0.5
                pos *= 0.5; tgt_hit += 1
        if j - entry_i >= plan["max_hold_bars"]:
            out_price = float(df.open.iloc[j + 1]) if j + 1 < len(df) else float(b.close)
            pnl += (sgn * (ent - out_price) if sgn == -1 else sgn * (out_price - ent)) * pos
            return {"state": "CLOSED", "exit": "超时", "pnl_usdt": pnl, "trig": trig_kind}
    last = df.iloc[-1]
    unreal = (sgn * (ent - last.close) if sgn == -1 else sgn * (last.close - ent)) * pos
    return {"state": "POSITION", "entry": ent, "trig": trig_kind, "qty0": qty,
            "tgt_hit": tgt_hit, "unreal_usdt": round(pnl + unreal, 3),
            "worst_now_usdt": -risk, "invalid": plan["invalid"]}


def main():
    plans = json.load(open(PLANS_PATH, encoding="utf-8"))["plans"]
    out_lines = []
    for plan in plans:
        df = frame(plan["symbol"])
        det = EventDetector(Params.from_json(PARAMS_PATH))
        evs = []
        for b in df.itertuples():
            evs += det.update(dict(ts=int(b.ts), open=b.open, high=b.high, low=b.low,
                                   close=b.close, volume=b.volume))
        res = replay_plan(plan, df, evs)
        res.update(plan_id=plan["plan_id"], asof=str(pd.Timestamp(df.ts.iloc[-1], unit="ms", tz="UTC")))
        print(plan["plan_id"], "→", json.dumps(res, ensure_ascii=False))
        out_lines.append(json.dumps(res, ensure_ascii=False))
    LOG_PATH.parent.mkdir(exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        for l in out_lines:
            f.write(l + "\n")


if __name__ == "__main__":
    main()
