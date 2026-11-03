"""主张身份(C5 第 1 步):价格相同不等于同一机会,对象重建不等于换机会。

第 1 步只加身份与透传,不改任何判断;因此这里的断言全部是"身份"层面的,
回放必须逐位不变(见 reports/identity_20261103/README.md)。
"""

from __future__ import annotations

from market_operator.structure import Zone, ZoneRole, ZoneState
from market_operator.events import EventDetector
from market_operator.structure import Params


def test_clone_preserves_claim_identity():
    zone = Zone(101.0, 103.0, ZoneRole.RESISTANCE, box_id=7, geometry_version=2)
    copy = zone.clone()
    assert copy.claim_id == zone.claim_id
    assert copy.zone_uid == zone.zone_uid
    assert copy.box_id == 7 and copy.geometry_version == 2


def test_same_price_but_different_box_is_a_different_claim():
    a = Zone(101.0, 103.0, ZoneRole.RESISTANCE, box_id=7)
    b = Zone(101.0, 103.0, ZoneRole.RESISTANCE, box_id=8)
    assert a.claim_id != b.claim_id


def test_same_box_but_new_geometry_version_is_a_different_claim():
    old = Zone(101.0, 103.0, ZoneRole.RESISTANCE, box_id=7, geometry_version=0)
    new = Zone(101.0, 103.5, ZoneRole.RESISTANCE, box_id=7, geometry_version=1)
    assert old.claim_id != new.claim_id


def test_role_is_part_of_identity():
    sup = Zone(101.0, 103.0, ZoneRole.SUPPORT, box_id=7)
    res = Zone(101.0, 103.0, ZoneRole.RESISTANCE, box_id=7)
    assert sup.claim_id != res.claim_id


def test_balance_boxes_carry_a_stable_box_id_through_refreshes():
    """生产结构里由平衡箱生成的区域必须带 box_id,且小幅刷新不改身份。"""
    from tests.test_v22_pipeline import _make_box_bars

    detector = EventDetector(Params(use_balance_detector=True))
    bars = _make_box_bars(cycles=4)
    for bar in bars:
        detector.update(bar)
    zones = [z for z in detector.structure._zones.zones
             if z.state is ZoneState.ACTIVE]
    assert zones, "平衡箱应生成至少一个区域"
    assert all(z.box_id is not None for z in zones)
    before = {id(z): z.claim_id for z in zones}
    detector.update(bars[-1])            # 同一根重放:属性刷新,不是换机会
    after = [z for z in detector.structure._zones.zones
             if z.state is ZoneState.ACTIVE]
    assert [z.claim_id for z in after] == [before[id(z)] for z in after]
