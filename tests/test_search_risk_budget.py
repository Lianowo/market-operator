import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from search_risk_budget import candidates, shortlist, passes


def test_grid_is_bounded_and_unique():
    configs = candidates()
    assert len(configs) == len({c["id"] for c in configs}) == 31
    assert all(0 < c["risk_frac"] <= .04 for c in configs)


def test_shortlist_uses_dev_only_and_hard_budget():
    results = {"a": {"dev": {"equity_end": 700, "max_drawdown": .15}},
               "b": {"dev": {"equity_end": 900, "max_drawdown": .151}},
               "c": {"dev": {"equity_end": 800, "max_drawdown": .14}},
               "d": {"dev": {"equity_end": 299, "max_drawdown": .01}}}
    assert shortlist(results, .15) == ["c", "a"]


def test_validation_requires_all_scenarios_and_profit():
    good = {k: {"equity_end": 500, "max_drawdown": .15}
            for k in ("dev", "test", "full", "stress")}
    assert passes(good, .15)
    good["stress"]["max_drawdown"] = .151
    assert not passes(good, .15)
    good["stress"] = {"equity_end": 299, "max_drawdown": .1}
    assert not passes(good, .15)
    del good["stress"]
    assert not passes(good, .15)