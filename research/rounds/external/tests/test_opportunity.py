"""Structural-area causal trade lifecycle; no arbitrary profitability gates."""
from market_operator.events import E1,E2,E3,E4,E5,E6,E7,MarketEvent
from market_operator.hypothesis import StructureView
from research.rounds.external.market_areas import AreaBook
from research.rounds.external.opportunity import OpportunityRouter
from market_operator.structure import Regime, Zone, ZoneRole, ZoneState

T0=1700000000000
H=3600000


def bar(i,close,high=None,low=None):
    return (T0+i*H,close,high if high is not None else close+.3,
            low if low is not None else close-.3,close,1)


def view(regime=Regime.TRANSITION, zone=None):
    return StructureView(regime, () if zone is None else (zone,), 1)


def ev(eid,i,d=-1,ref=100,invalid=101,**extra):
    return MarketEvent(eid,d,T0+i*H,i,{"ref":ref,"invalid":invalid,**extra})


def step(r,i,close,evs=(),context=None,high=None,low=None,
         holding=False,resting=False):
    return r.on_bar("BTC",bar(i,close,high,low),i,
                    view() if context is None else context,1,evs,
                    position_open=holding,entry_resting=resting)


def break_zone():
    return Zone(100,100.5, ZoneRole.SUPPORT, ZoneState.BROKEN,
                origin_known=0)


def flip_zone():
    return Zone(100,100.5, ZoneRole.RESISTANCE, ZoneState.FLIPPED,
                origin_known=0)


def trend_event(i,d=-1,pb=100,leg=98):
    return ev(E4,i,d,ref=99,invalid=101, pb_price=pb,leg_start=leg,
              regime=Regime.TREND_DOWN.value)


def range_event(i,d=-1):
    zone=Zone(101,102,ZoneRole.RESISTANCE,origin_known=0)
    return ev(E5,i,d,ref=101,invalid=103,band=(95,105),zone=zone)


def test_e1_and_e6_breaks_are_observations_only():
    r=OpportunityRouter()
    zone=break_zone()
    assert step(r,0,99,[ev(E6,0,pivot_forming=0)],context=view(zone=zone))==[]
    assert r.areas.active["BTC"][0].stage=="BROKEN"
    assert step(r,1,99,high=100.1,low=98.9)==[]
    assert r.areas.active["BTC"][0].stage=="PROBED"
    assert step(r,2,99,[ev(E1,2,zone=zone)],context=view(zone=zone))==[]
    assert r.counters["break_observed"]==2
    assert r.counters.get("confirmed_entry",0)==0


def test_e7_without_prior_same_area_break_is_only_a_local_flip():
    r=OpportunityRouter()
    assert step(r,3,99,[ev(E7,3,zone=flip_zone())])==[]
    assert r.counters["unanchored_flip"]==1
    assert step(r,4,98.6)==[]
    assert r.counters.get("confirmed_entry",0)==0


def test_e7_after_known_break_and_probe_requires_another_close():
    r=OpportunityRouter()
    z=break_zone()
    step(r,0,99,[ev(E6,0,pivot_forming=0)],context=view(zone=z))
    step(r,1,99,high=100.2,low=98.7)
    assert r.areas.active["BTC"][0].stage=="PROBED"
    assert step(r,2,99,[ev(E7,2,zone=flip_zone())])==[]
    pending=r.opportunities["BTC"]
    assert pending.stage=="RETEST_REJECTED"
    assert pending.scale_bars==2
    decision=step(r,3,98.5,context=None)
    assert len(decision)==1
    assert decision[0].kind=="market"
    assert decision[0].route=="area_confirmed"
    assert decision[0].meta["source_event"]==E7
    assert decision[0].meta["structure_span_bars"]==2
    assert r.areas.active["BTC"]==[]
    assert len(r.consumed)==1


def test_e7_requires_a_break_of_exactly_this_area():
    r=OpportunityRouter()
    far_zone=Zone(110,111, ZoneRole.SUPPORT,origin_known=0)
    step(r,0,108,[ev(E6,0,ref=110,invalid=112,pivot_forming=0)],
         context=view(zone=far_zone))
    step(r,2,99,[ev(E7,2,zone=flip_zone())])
    assert r.counters["unanchored_flip"]==1
    assert not r.opportunities


def test_same_bar_e6_and_e7_do_not_fake_a_historical_retest():
    r=OpportunityRouter()
    z=break_zone()
    step(r,0,99,[ev(E6,0,pivot_forming=0),ev(E7,0,zone=flip_zone())],
         context=view(zone=z))
    assert r.counters.get("confirmed_entry",0)==0
    assert r.counters["unanchored_flip"]==1
    assert step(r,1,98)==[]


def test_reclaimed_break_does_not_resurrect_at_later_flip():
    r=OpportunityRouter()
    step(r,0,99,[ev(E6,0,pivot_forming=0)],context=view(zone=break_zone()))
    step(r,1,102,high=102.3,low=99)
    assert r.areas.active["BTC"]==[]
    assert r.areas.history["BTC"][-1][1]=="RECLAIMED"
    step(r,2,99,[ev(E7,2,zone=flip_zone())])
    assert r.counters["unanchored_flip"]==1


def test_e2_e3_local_reactions_are_not_trade_ideas():
    r=OpportunityRouter()
    for i,eid in enumerate((E2,E3)):
        assert step(r,i,99,[ev(eid,i)])==[]
    assert not r.opportunities
    assert r.counters["local_rejection_observed_E2"]==1
    assert r.counters["local_rejection_observed_E3"]==1


def test_e4_trend_continuation_has_structural_identity_and_single_campaign():
    r=OpportunityRouter()
    trend_view=view(Regime.TREND_DOWN)
    # First establish a real break, rather than treating a 2-pivot recovery
    # in isolation as an entire directional campaign.
    step(r,0,99,[ev(E6,0,pivot_forming=0)],context=view(zone=break_zone()))
    assert step(r,1,99,[trend_event(1)],context=trend_view)==[]
    first=step(r,2,98.5)
    assert len(first)==1 and first[0].hyp.direction==-1
    assert first[0].hyp.target_1 is None
    # A replay of the SAME defended pullback must not open another trade.
    assert step(r,3,98,[trend_event(3)],context=trend_view)==[]
    assert r.counters["previously_consumed_area"]==1
    assert step(r,4,97.5)==[]
    assert r.counters["confirmed_entry"]==1
    # A genuinely different confirmed pullback starts a new campaign.
    step(r,5,97,[trend_event(5,pb=99,leg=97)],context=trend_view)
    assert len(step(r,6,96.7))==1
    assert r.counters["confirmed_entry"]==2


def test_e4_without_trend_context_never_becomes_trade():
    r=OpportunityRouter()
    assert step(r,0,99,[trend_event(0)],context=view(Regime.TRANSITION))==[]
    assert r.counters["inconsistent_market_context"]==1
    assert step(r,1,98)==[]


def test_e5_range_rotation_uses_band_and_not_external_score():
    r=OpportunityRouter()
    rng=view(Regime.RANGE)
    assert step(r,0,100,[range_event(0)],context=rng)==[]
    assert r.opportunities["BTC"].stage=="REACTION_PENDING"
    ds=step(r,1,99.8)
    assert len(ds)==1 and ds[0].meta["source_event"]==E5
    assert ds[0].hyp.target_1==95


def test_failed_retest_clears_candidate_and_does_not_dispatch():
    r=OpportunityRouter()
    step(r,0,99,[ev(E6,0,pivot_forming=0)],context=view(zone=break_zone()))
    step(r,1,99,[ev(E7,1,zone=flip_zone())])
    assert step(r,2,101.5)==[]
    assert "BTC" not in r.opportunities
    assert r.counters["thesis_failed"]==1


def test_competing_reactions_same_bar_abstains_without_hidden_priority():
    r=OpportunityRouter()
    rng=view(Regime.RANGE)
    event_a=range_event(0)
    event_b=ev(E7,0,zone=flip_zone())
    assert step(r,0,99,[event_a,event_b],context=rng)==[]
    assert r.counters["competing_areas"]==1
    assert not r.opportunities


def test_long_break_and_retest_are_symmetric():
    r=OpportunityRouter()
    z=Zone(99.5,100,ZoneRole.RESISTANCE,origin_known=0)
    flipped=Zone(99.5,100,ZoneRole.SUPPORT,origin_known=0)
    step(r,0,101,[ev(E6,0,1,ref=100,invalid=99,pivot_forming=0)],
         context=view(zone=z))
    step(r,1,101,high=101.2,low=99.7)
    step(r,2,101,[ev(E7,2,1,ref=100,invalid=99,zone=flipped)])
    ds=step(r,3,101.5)
    assert len(ds)==1 and ds[0].hyp.direction==1


def test_pullback_scale_is_observed_pivot_formation_time_not_fixed_bars():
    from market_operator.structure import Pivot, PivotKind
    r=OpportunityRouter()
    known_pivots=(
        Pivot(98.0,forming_index=2,known_index=4,kind=PivotKind.LOW),
        Pivot(100.0,forming_index=8,known_index=9,kind=PivotKind.HIGH))
    v=StructureView(Regime.TREND_DOWN, (), 1, pivots=known_pivots)
    step(r,0,99,[ev(E6,0,pivot_forming=0)],context=view(zone=break_zone()))
    assert step(r,10,99,[trend_event(10)],context=v)==[]
    o=r.opportunities["BTC"]
    assert o.scale_bars==6  # actual 8-2 formation span; NOT a 6-bar rule
    dec=step(r,11,98.5)
    assert dec[0].meta["structure_span_bars"]==6
    assert dec[0].hyp.target_1 is None


def test_isolated_e4_without_prior_structural_expansion_is_not_a_trade():
    r=OpportunityRouter()
    trend_view=view(Regime.TREND_DOWN)
    assert step(r,0,99,[trend_event(0)],context=trend_view)==[]
    assert r.counters["trend_without_prior_expansion"]==1
    assert step(r,1,98.5)==[]
    assert r.counters.get("confirmed_entry",0)==0


def test_local_trend_pivot_has_no_expansion_beyond_last_broken_defended_level():
    r=OpportunityRouter()
    step(r,0,99,[ev(E6,0,pivot_forming=0)],context=view(zone=break_zone()))
    trend_view=view(Regime.TREND_DOWN)
    # Leg low 100.2 is still above old support at 100; no directional
    # expansion regardless of the E4 event label.
    step(r,1,100,[trend_event(1,pb=101,leg=100.2)],context=trend_view)
    assert r.counters["trend_without_prior_expansion"]==1
    assert "BTC" not in r.opportunities


def test_market_structure_observation_continues_during_real_position():
    router=OpportunityRouter()
    # A trader may already hold a position when a genuinely new defended
    # structural area breaks. Cognition cannot stop because order-taking
    # was disabled. A later exit must not erase the market's actual history.
    assert step(router,10,99,
                [ev(E6,10,pivot_forming=2)],
                context=view(zone=break_zone()),
                holding=True)==[]
    assert len(router.areas.active["BTC"])==1
    assert router.counters["break_observed"]==1
    assert step(router,11,99,
                [ev(E2,11)],
                holding=True)==[]
    assert router.counters["local_rejection_observed_E2"]==1
    assert step(router,12,99,[ev(E7,12,zone=flip_zone())])==[]
    assert router.opportunities["BTC"].stage=="RETEST_REJECTED"
    accepted=step(router,13,98.7)
    assert len(accepted)==1 and accepted[0].meta["source_event"]==E7
