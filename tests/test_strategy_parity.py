"""Exact-output verification must not hide numeric, order, or accounting changes."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def parity():
    path = Path(__file__).resolve().parents[1] / "tools" / "verify_strategy_parity.py"
    spec = importlib.util.spec_from_file_location("verify_strategy_parity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fingerprint_ignores_dictionary_insertion_order(parity):
    assert parity._fingerprint({"a": 1, "b": 2}) == parity._fingerprint({"b": 2, "a": 1})


@pytest.mark.parametrize("changed", [
    [{"price": 100.00000001}, {"price": 101.0}],
    [{"price": 101.0}, {"price": 100.0}],
    [{"price": 100.0, "fee": 0.1}, {"price": 101.0}],
])
def test_fingerprint_preserves_precision_order_and_all_fields(parity, changed):
    original = [{"price": 100.0}, {"price": 101.0}]
    assert parity._fingerprint(original) != parity._fingerprint(changed)


def test_fingerprint_rejects_nonfinite_outputs(parity):
    with pytest.raises(ValueError):
        parity._fingerprint({"equity": float("nan")})