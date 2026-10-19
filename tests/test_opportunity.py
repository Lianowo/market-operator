"""Opportunity is a causal process, never a synonym for one event."""
from market_operator.events import MarketEvent, E1, E6, E7
from market_operator.hypothesis import StructureView
from market_operator.opportunity import OpportunityRouter
from market_operator.structure import Regime

T0=1700000000000
H=3600000


def bar(i, close, high=None, low=None):
    return (T0+i*H, close, high if high is not None else close+0.3,
            low if low is not None else close-0.3, close, 1)


def view():
    return StructureView(regime=Regime.TRANSITION, zones=(), atr=1)


def event(id_, i, direction=-1, ref=100, invalid=102):
    return MarketEvent(id=id_, direction=direction,
                       ts=T0+i*H, known_index=i,
                       attrs={"ref":ref,"invalid":invalid,"atr":1})


def drive(router,i,price,evs=(),**kw):
    return router.on_bar("BTC",bar(i,price),i,view(),1,evs,
                         position_open=kw.get("holding",False),
                         entry_resting=kw.get("resting",False))


def test_e1_and_e6_are_market_facts_not_direct_orders():
    r=OpportunityRouter()
    assert drive(r,0,99,[event(E6,0)]) == []
    assert r.opportunities["BTC"].stage=="BREAK_OBSERVED"
    assert drive(r,1,98,[event(E1,1)]) == []
    assert r.opportunities["BTC"].stage=="BREAK_OBSERVED"
    assert r.counters["break_observed"]==2
    assert drive(r,2,96)==[]


def test_same_bar_e7_not_a_confirmed_market_entry():
    r=OpportunityRouter()
    assert drive(r,3,99,[event(E7,3,ref=100,invalid=102)])==[]
    assert r.opportunities["BTC"].stage=="RETEST_REJECTED"
    # A distinct later close which actually extends away from zone confirms.
    ds=drive(r,4,98.4)
    assert len(ds)==1 and ds[0].kind=="market"
    assert ds[0].intent.reason.startswith("E7|short RETEST_CONFIRMED")
    assert ds[0].meta["confirmation_index"]==4
    assert ds[0].hyp.target_1 is None  # no fake fixed-R target
    assert r.opportunities=={}


def test_retest_failure_and_reentry_revoke_hypothesis_without_trade():
    r=OpportunityRouter()
    drive(r,0,99,[event(E7,0)])
    assert drive(r,1,102.5)==[]
    assert r.opportunities=={}
    assert r.counters["retest_failed"]==1
    drive(r,2,99,[event(E7,2)])
    assert drive(r,3,100.5)==[]
    assert r.opportunities=={}
    assert r.counters["retest_reentered"]==1


def test_no_fabricated_direction_when_retest_stalls():
    r=OpportunityRouter()
    drive(r,0,99,[event(E7,0)])
    assert drive(r,1,99)==[]
    assert drive(r,2,99.2)==[]
    ds=drive(r,3,98.7)
    assert len(ds)==1 and ds[0].meta["confirmation_index"]==3


def test_symmetric_long_side():
    r=OpportunityRouter()
    drive(r,5,101,[event(E7,5,direction=1,ref=100,invalid=98)])
    ds=drive(r,6,101.1)
    assert len(ds)==1
    assert ds[0].hyp.direction==1


def test_position_blocks_redundant_pending_thesis():
    r=OpportunityRouter()
    drive(r,0,99,[event(E7,0)])
    assert drive(r,1,98,[event(E1,1)],holding=True)==[]
    assert r.opportunities=={}
