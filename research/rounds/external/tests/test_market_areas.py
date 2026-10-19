"""Region identity and causal episode behavior, not profitability filters."""
from market_operator.events import E1,E6,E7,MarketEvent
from research.rounds.external.market_areas import AreaBook
from market_operator.structure import Zone,ZoneRole,ZoneState

BASE=1700000000000
H=3600000


def event(eid,t,ref=100,invalid=101,zone=None):
    attrs={"ref":ref,"invalid":invalid}
    if zone is not None:
        attrs["zone"]=zone
    if eid==E6:
        attrs["pivot_forming"]=max(0,t-17)
    return MarketEvent(eid,-1,BASE+t*H,t,attrs)


def support(lo=100,hi=100.5,origin=1):
    return Zone(lo,hi,ZoneRole.SUPPORT,ZoneState.BROKEN,origin_known=origin)


def flip(lo=100,hi=100.5,origin=1):
    return Zone(lo,hi,ZoneRole.RESISTANCE,ZoneState.FLIPPED,
                origin_known=origin)


def test_break_and_retest_same_region_not_identity_by_role():
    book=AreaBook()
    a=book.observe_break("BTC",event(E6,20),zones=(support(),))
    assert a.area.origin_known==1
    assert a.area.span_bars==19  # factual span, no selection threshold
    book.on_bar("BTC",21,100.2,98.9,99)
    assert a.stage=="PROBED"
    match=book.matched_retest("BTC",event(E7,22,zone=flip()))
    assert match is a
    book.consume("BTC",a,23)
    assert book.active["BTC"]==[]
    assert book.history["BTC"][-1][1]=="CONSUMED"


def test_different_region_same_side_cannot_reuse_broken_area():
    book=AreaBook()
    book.observe_break("BTC",event(E6,20),zones=(support(),))
    assert book.matched_retest(
        "BTC",event(E7,22,ref=90,invalid=91,zone=flip(90,90.5))) is None


def test_no_future_zone_can_backfill_missing_past_area():
    book=AreaBook()
    first=book.observe_break("BTC",event(E6,25),zones=())
    assert first.area.lo==100 and first.area.hi==100
    assert first.area.origin_known==8
    book.on_bar("BTC",26,100.2,99.8,99.9)
    # The later derived width does not rewrite the historical E6.
    assert first.area.lo==100 and first.area.hi==100


def test_thesis_stays_alive_over_irregular_time_without_fixed_k_window():
    book=AreaBook()
    a=book.observe_break("BTC",event(E6,20),zones=(support(),))
    for t in range(21,200):
        book.on_bar("BTC",t,99.8,98,99)
    assert book.active["BTC"]==[a]
    assert a.last_known==199
    assert book.matched_retest("BTC",event(E7,200,zone=flip())) is a


def test_reclaim_through_invalidation_fails_original_campaign():
    book=AreaBook()
    book.observe_break("BTC",event(E6,20),zones=(support(),))
    book.on_bar("BTC",21,101.5,99,101.2)
    assert book.active["BTC"]==[]
    assert book.history["BTC"][-1][1]=="RECLAIMED"
    assert book.matched_retest("BTC",event(E7,22,zone=flip())) is None


def test_same_area_new_break_replaces_stale_episode_deterministically():
    book=AreaBook()
    a=book.observe_break("BTC",event(E1,20,zone=support()),zones=())
    b=book.observe_break("BTC",event(E6,22),zones=(support(),))
    assert a is not b
    assert book.active["BTC"]==[b]
    assert b.area.break_known==22


def test_two_overlapping_breaks_do_not_pick_a_profitable_winner():
    book=AreaBook()
    book.observe_break("BTC",event(E6,20,ref=99.7,invalid=101),
                       zones=(support(99.5,99.8),))
    book.observe_break("BTC",event(E6,21,ref=100.3,invalid=101.2),
                       zones=(support(100.2,100.5,origin=10),))
    # Two distinct areas can both match a broad retest; no silent ranking.
    ambiguous=event(E7,22,zone=flip(99.6,100.4))
    assert book.matched_retest("BTC",ambiguous) is None
