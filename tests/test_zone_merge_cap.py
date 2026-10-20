"""区域合并封顶修复测试(2026-10-20, research-log #15)。

动机:无界链式并集在真实两年数据上滚出[58680,125338]级"巨区",吞掉全部细结构。
约束:默认值 0 必须保留历史无界行为(v1-v8 parity 不受影响)。
"""
import sys

sys.path.insert(0, "src")

from market_operator.structure import Params, Pivot, PivotKind, ZoneBook


def _mk_pivot(price):
    return Pivot(price=price, forming_index=0, known_index=0, kind=PivotKind.HIGH)


def _hp(book, prices, atr):
    for i, p in enumerate(prices):
        book.on_pivot(_mk_pivot(p), i, atr_val=atr)


SERIES = [100.0, 100.9, 101.8, 102.7, 103.6]   # atr=2 → 区宽 w=1, 逐步搭接


def test_merge_within_cap_partially_merges():
    zb = ZoneBook(Params(merge_max_atr=2.0))          # cap=2*2=4
    _hp(zb, SERIES, atr=2.0)
    # 三次并入至[99,102.7](宽3.7≤4),最后一跳并集会达4.6>4 → 不并
    assert len(zb.zones) == 2
    big = max(zb.zones, key=lambda z: z.hi - z.lo)
    assert abs(big.lo - 99.0) < 1e-9 and abs(big.hi - 102.7) < 1e-9


def test_legacy_default_unbounded_full_chain():
    zb = ZoneBook(Params())                            # cap=0 历史行为
    _hp(zb, SERIES, atr=2.0)
    assert len(zb.zones) == 1
    assert abs(zb.zones[0].lo - 99.0) < 1e-9 and abs(zb.zones[0].hi - 103.6) < 1e-9


def test_cap_prevents_mega_zone_but_keeps_fine_structure():
    capped = ZoneBook(Params(merge_max_atr=3.0))       # cap=3*0.5=1.5(小ATR)
    _hp(capped, [100.0 + 0.4 * i for i in range(40)], atr=0.5)
    assert all(z.hi - z.lo <= 1.5 for z in capped.zones)
    assert len(capped.zones) > 3                       # 细结构存活,非单一巨区
