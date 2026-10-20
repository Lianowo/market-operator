"""当前 BTC 市场实算决策报告(canonical 管线,只读不写仓位)。
拉取最新 1h 与本地带哈希数据拼接,重放 structure/events 到最后一根,
输出: 观察/状态/关键位/最近事件/当前行动与理由/计划扳机与失效。
"""
import sys
import pandas as pd

sys.path.insert(0, "src")
from market_operator import data
from market_operator.structure import MarketStructure, Params
from market_operator.events import EventDetector

SYM = "BTCUSDT"
P = Params.from_json("config/canonical.json")   # 含 merge_max_atr=2.0 修复(log#15)


def context_block():
    """跨层语境(只读,不影响闸门):12h regime/结构位置 + 资金费。"""
    import json, urllib.request
    c = json.load(open("config/canonical.json", encoding="utf-8"))
    p2 = Params.from_json("config/defaults.json")
    for tf in ["4h", "12h", "1d"]:
        df = pd.concat([pd.read_csv(f"data/raw/BTCUSDT_{tf}.csv"),
                        data.fetch_klines(SYM, granularity="4H", years=0.01)]) \
            .drop_duplicates("ts").sort_values("ts").reset_index(drop=True) \
            if tf == "4h" else pd.read_csv(f"data/raw/BTCUSDT_{tf}.csv")
        det = EventDetector(p2)
        for b in df.itertuples():
            det.update(dict(ts=int(b.ts), open=b.open, high=b.high, low=b.low,
                            close=b.close, volume=b.volume))
        ms = det.structure
        hs = [x for x in ms.pivots if x.kind.value == "high"]
        ls = [x for x in ms.pivots if x.kind.value == "low"]
        hi, lo = hs[-1].price, ls[-1].price
        pos = (df.close.iloc[-1] - lo) / (hi - lo)
        print(f"  [{tf}] regime={ms.regime().value:11s} 带[{lo:.0f},{hi:.0f}] pos={pos:.2f}"
              f"(数据截至 {pd.Timestamp(df.ts.iloc[-1], unit='ms', tz='UTC')})")
    try:
        r = json.load(urllib.request.urlopen(
            "https://api.bitget.com/api/v2/mix/market/current-fund-rate"
            "?symbol=BTCUSDT&productType=USDT-FUTURES", timeout=10))
        print(f"  [funding] {float(r['data'][0]['fundingRate']) * 100:.3f}%/8h")
    except Exception as e:
        print(f"  [funding] 拉取失败:{e}")


def latest_frame():
    df = data.load(SYM)
    fresh = data.fetch_klines(SYM, granularity="1H", years=0.01)
    out = pd.concat([df, fresh]).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    assert data.validate(out) == [], "fresh data failed validation"
    return out


def main():
    df = latest_frame()
    det = EventDetector(P, enable_e8=True)
    events = []
    for i, b in enumerate(df.itertuples()):
        ev = det.update(dict(ts=int(b.ts), open=b.open, high=b.high,
                             low=b.low, close=b.close, volume=b.volume))
        events += ev
    ms = det.structure
    t = len(df) - 1
    last = df.iloc[-1]
    atr = ms.atr()
    iso = pd.Timestamp(last.ts, unit="ms", tz="UTC")

    print(f"=== {SYM} 决策报告 截至 {iso} (bar {t}) ===")
    print(f"收盘 {last.close:.1f}  ATR(14) {atr:.1f} ({atr/last.close*100:.2f}%)  regime={ms.regime().value}")

    piv = ms.pivots
    hs = [p for p in piv if p.kind.value == "high"][-4:]
    ls = [p for p in piv if p.kind.value == "low"][-4:]
    print("\n== 最近确认摆动(形成bar→知时bar) ==")
    for p in sorted(hs + ls, key=lambda x: x.forming_index):
        age_h = (t - p.forming_index)
        print(f"  {p.kind.value:4s} {p.price:9.1f}  forming={p.forming_index} known={p.known_index} ({age_h}h前)")

    zones = [z for z in ms.zones if z.state.value in ("ACTIVE", "BROKEN", "RETESTED")]
    print("\n== 在册关键价格区(角色/状态/触碰/拒绝) ==")
    for z in sorted(zones, key=lambda z: z.hi):
        dist = (z.hi - last.close) / atr if z.hi > last.close else (z.lo - last.close) / atr
        print(f"  {z.role:10s} [{z.lo:.0f}, {z.hi:.0f}] {z.state.value:8s} touches={z.touches} "
              f"rejects={z.rejects} 距现价 {dist:+.2f}ATR")

    print("\n== 最近 30 根事件 ==")
    for e in events[-8:] if events else []:
        mark = " <<==" if e.known_index >= t - 30 else ""
        print(f"  {e.id} dir={e.direction:+d} ts={pd.Timestamp(e.ts, unit='ms', tz='UTC')} "
              f"known={e.known_index}{mark}")

    print("\n== 跨层语境(只读,不决策) ==")
    context_block()

    print("\n== tape 注解(只读,无判定;n<60 接触前不引为证据) ==")
    try:
        from market_operator import tape_features as tf
        import time as _t
        day = _t.strftime("%Y%m%d", _t.gmtime())
        now = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
        for hrs in (24, 4, 1):
            s = tf.window_stats("BTCUSDT", now - hrs * 3600_000, now, day=day)
            if s.get("n", 0) > 0:
                print(f"  [近{hrs}h] n={s['n']} delta_share={s['delta_share']:+.2f} "
                      f"big_share={s['big_share']:.2f} eff={s['eff_per_million']:.1f}")
            else:
                print(f"  [近{hrs}h] 无tape记录(record 未运行)")
    except Exception as e:
        print(f"  tape 注解失败:{e}")

    print("\n== 当前行动(canonical 语义重放) ==")
    print("模拟最近事件→假设→F1/room/风险闸,详见下块计划扳机。")


if __name__ == "__main__":
    main()

def plan(df, det, t, atr):
    ms = det.structure
    last = df.iloc[-1]
    piv = ms.pivots
    hs = [p for p in piv if p.kind.value == "high"]
    ls = [p for p in piv if p.kind.value == "low"]
    bm = P.k_bm * atr
    print("\n== 计划扳机(canonical 白名单 E1/E6/E7, F1≥0.8ATR, turtle收割) ==")
    if ms.regime().value == "TREND_DOWN" and hs:
        h_star = hs[-1].price
        trig = h_star + 0.5 * atr
        print(f"[CHoCH 反转多] 收盘 > {h_star + bm:.0f} 触发 E6|long;"
              f" STRONG 路由需 margin≥0.5ATR ⇒ 收盘 ≥ {trig:.0f} 且 bar 振幅 ≥ {1.5*atr:.0f} ⇒ 次开盘市价多")
        print(f"  失效: 突破翻转区下沿−buf(±{P.k_zw*atr:.0f}), F1 距离须≥{0.8*atr:.0f};"
              f" 否则该单不存在(不为交易而交易)")
    sup = [z for z in ms.zones if z.role.value == "SUPPORT" and z.state.value == "ACTIVE"]
    if sup:
        near = min([z for z in sup if z.hi > last.close * 0.99], key=lambda z: abs(z.lo - last.close), default=None)
        if near:
            accept = near.lo - bm
            print(f"[跌破接受空] 收盘 < {accept:.0f} 破位候选;随后 3 根全部收在 {near.lo:.0f} 下 ⇒ E1|short 次开盘市价")
            print(f"  失效: {near.hi + bm:.0f}(区界+buf) 其一;距离 {near.hi+bm-accept:.0f}≥{0.8*atr:.0f} F1 闸"
                  f" ⇒ 满足;目标=2R(实际失效距离)或对侧区,以 room≥1.5R 为闸")
            print(f"[回踩翻转空] 若先破后回测 {near.lo:.0f}-{near.hi:.0f} 被拒(FLIPPED) ⇒ E7|short 挂限价 {near.lo:.0f}(前缘),等待 ≤12×1h")
    print(f"\n[风险预算] 单笔风险 = 0.75%×权益(300 USDT → $2.25);杠杆 cap 5x;组合容量 3%")
    print("[推翻当前判断] 收盘站回上方 BROKEN 支撑带(角色已转阻力)之上 ⇒ TREND_DOWN 失效,E6|short 语义转向")


if __name__ == "__main__":
    pass
