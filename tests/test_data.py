import hashlib
import json

import pandas as pd
import pytest

from market_operator import data

STEP = 3_600_000
T0 = 1_700_000_000_000


def make_df(n: int = 24) -> pd.DataFrame:
    ts = [T0 + i * STEP for i in range(n)]
    return data.pd.DataFrame(
        {
            "ts": ts,
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [10.0] * n,
        }
    )


def test_validate_clean_passes():
    assert data.validate(make_df()) == []


def test_validate_detects_gap():
    df = make_df(10)
    df.loc[5:, "ts"] += STEP
    issues = data.validate(df)
    gaps = [i for i in issues if i.startswith("gap:")]
    assert len(gaps) == 1
    assert "1 bars missing" in gaps[0]


def test_validate_detects_duplicates():
    df = pd.concat([make_df(6), make_df(6).iloc[[3]]], ignore_index=True)
    issues = data.validate(df)
    assert any(i.startswith("duplicate ts:") and str(T0 + 3 * STEP) in i for i in issues)


@pytest.mark.parametrize(
    "col,val",
    [("low", 105.0), ("open", 96.0), ("close", 108.0)],
)
def test_validate_detects_bad_ohlc(col, val):
    df = make_df()
    df.loc[4, col] = val
    issues = data.validate(df)
    assert any("bad OHLC" in i and str(T0 + 4 * STEP) in i for i in issues)


def test_validate_detects_nonpositive_volume():
    df = make_df()
    df.loc[2, "volume"] = 0.0
    issues = data.validate(df)
    assert any("non-positive volume" in i for i in issues)


def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "DATA_DIR", tmp_path)
    monkeypatch.setattr(data, "MANIFEST_PATH", tmp_path / "manifest.json")
    df = make_df(10)

    path = data.save(df, "TESTUSDT")
    assert path.exists()
    assert list(pd.read_csv(path).columns) == data.COLUMNS

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    rec = manifest["files"]["TESTUSDT_1h.csv"]
    assert rec["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert rec["rows"] == 10
    assert rec["ts_first"] == T0
    assert rec["ts_last"] == T0 + 9 * STEP
    assert rec["validation_issues"] == []
    assert rec["span_days"] == round(9 * STEP / 86_400_000, 2)
    assert data.load("TESTUSDT").equals(df)


def test_load_sorts_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "DATA_DIR", tmp_path)
    df = make_df(6)
    dirty = pd.concat([df, df.iloc[[2]]], ignore_index=True).sample(frac=1.0, random_state=0)
    (tmp_path / "raw").mkdir()
    dirty.to_csv(tmp_path / "raw" / "TESTUSDT_1h.csv", index=False)
    assert data.load("TESTUSDT").equals(df)


def test_save_records_validation_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "DATA_DIR", tmp_path)
    monkeypatch.setattr(data, "MANIFEST_PATH", tmp_path / "manifest.json")
    df = make_df(6)
    df.loc[3:, "ts"] += STEP
    data.save(df, "TESTUSDT")
    rec = json.loads((tmp_path / "manifest.json").read_text())["files"]["TESTUSDT_1h.csv"]
    assert any(i.startswith("gap:") for i in rec["validation_issues"])
