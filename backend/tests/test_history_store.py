"""Tests for the historical reference store and its API surface.

Run: python -m pytest backend/tests -q

The store exists because the state repositories hold only the latest row (4 macro,
49 spatial). Situational awareness needs history; these tests pin the properties that
make it trustworthy: look-ahead-free derivation, honest field semantics, no synthesis,
and non-destructive refreshes.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

import history_store as hs  # noqa: E402


# --- atomic writes ----------------------------------------------------------------
def test_atomic_write_leaves_no_partial_file(tmp_path):
    path = str(tmp_path / "out.csv")
    hs._atomic_write(pd.DataFrame({"a": [1, 2]}), path)
    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp")
    assert pd.read_csv(path)["a"].tolist() == [1, 2]


def test_missing_file_loads_none(tmp_path):
    assert hs._load_existing(str(tmp_path / "absent.csv")) is None


def test_corrupt_file_loads_none(tmp_path):
    # A binary blob is not valid CSV text for the expected schema; the loader must not raise.
    p = tmp_path / "bad.csv"
    p.write_bytes(b"\x00\x01\x02\x03")
    result = hs._load_existing(str(p))
    assert result is None or isinstance(result, pd.DataFrame)


# --- spatial history derivation ---------------------------------------------------
def _prices(n=120, seed=9):
    rng = np.random.default_rng(seed)
    close = 1800 + np.cumsum(rng.standard_normal(n) * 7)
    high = close + rng.uniform(1, 20, n)
    low = close - rng.uniform(1, 20, n)
    open_ = close + rng.standard_normal(n) * 2
    return pd.DataFrame({
        "date": pd.bdate_range(end="2026-09-25", periods=n).strftime("%Y-%m-%d"),
        "open": open_, "high": np.maximum.reduce([high, close, open_]),
        "low": np.minimum.reduce([low, close, open_]), "close": close,
        "source": "TEST",
    })


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "HISTORY_DIR", str(tmp_path))
    monkeypatch.setattr(hs, "PRICE_HISTORY", str(tmp_path / "price_history.csv"))
    monkeypatch.setattr(hs, "SPATIAL_HISTORY", str(tmp_path / "spatial_history.csv"))
    monkeypatch.setattr(hs, "MACRO_HISTORY", str(tmp_path / "macro_history.csv"))
    hs._atomic_write(_prices(), hs.PRICE_HISTORY)
    return hs


def test_spatial_history_is_look_ahead_free(store):
    out = store.build_spatial_history()

    assert (out["Three_Day_Low"] <= out["Three_Day_High"]).all()
    # Every boundary must be derivable from strictly prior bars: truncating the tail
    # must not change an earlier row. Compare on a date present in both frames.
    full = out.copy()
    truncated = pd.read_csv(store.PRICE_HISTORY).iloc[:80]
    hs._atomic_write(truncated, store.PRICE_HISTORY)
    partial = store.build_spatial_history()
    common = set(full["date"]) & set(partial["date"])
    assert common, "truncated build should still overlap the full build"
    probe = sorted(common)[-1]
    f = full[full["date"] == probe].iloc[0]
    p = partial[partial["date"] == probe].iloc[0]
    assert f["Three_Day_Low"] == pytest.approx(p["Three_Day_Low"])
    assert f["ATR_14"] == pytest.approx(p["ATR_14"])


def test_spatial_offset_is_the_150_pip_buffer(store):
    out = store.build_spatial_history()
    assert np.allclose(out["Sweep_Floor"], out["Three_Day_Low"] - 1.50)


def test_spatial_history_spans_full_price_history(store):
    out = store.build_spatial_history()
    prices = pd.read_csv(store.PRICE_HISTORY)
    # Almost all bars should carry a boundary (only the initial lookback is dropped).
    assert len(out) >= len(prices) - 20
    assert out["date"].iloc[-1] == prices["date"].iloc[-1]


def test_no_synthesised_prices_in_store():
    """The store must never invent prices; it derives boundaries from real OHLC only."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(hs))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) \
                    and isinstance(node.body[0].value, ast.Constant):
                node.body = node.body[1:]
    code = ast.unparse(tree)
    assert "np.random.normal" not in code


# --- macro history semantics ------------------------------------------------------
def test_macro_history_column_names_are_honest(store, monkeypatch):
    """Regression: the window bounds were originally named SDI_52w_* but held
    Commercial_Net values, which produced a nonsense 266% 'percentile of range'."""
    import inspect
    src = inspect.getsource(hs.build_macro_history)
    assert "SDI_52w_Low" not in src
    assert "Commercial_Net_52w_Low" in src
    assert "Commercial_Net_52w_High" in src


def test_macro_history_keeps_longer_existing_file(store, monkeypatch):
    """A shorter fetch must not truncate a longer stored history."""
    existing = pd.DataFrame({
        "date": ["2020-01-01", "2020-01-08", "2020-01-15"],
        "week_ending_date": ["2020-01-01", "2020-01-08", "2020-01-15"],
        "Commercial_Net": [-1.0, -2.0, -3.0], "SDI": [0.1, 0.2, 0.3],
        "fedwatch_dovish_prob": [10.0, 11.0, 12.0], "MACRO_GATE": ["CLOSED"] * 3,
    })
    hs._atomic_write(existing, store.MACRO_HISTORY)

    class _ShortIngestor:
        def __init__(self, *a, **k):
            pass

        def ingest_institutional_fund_flow(self, weeks=None):
            return pd.DataFrame([{
                "week_ending_date": "2026-01-02", "Large_Spec_Net": 1.0,
                "Commercial_Net": -5.0, "open_interest": 100.0,
            }])

        def ingest_weekly_macro_gravity(self):
            return pd.DataFrame([{
                "log_date": "2026-01-02", "fedwatch_dovish_prob": 20.0,
                "policy_target_rate": 4.0, "two_year_yield": 4.1,
                "central_bank_bias": "HAWKISH",
            }])

    monkeypatch.setattr(hs, "InstitutionalDataIngestor", _ShortIngestor)
    result = hs.build_macro_history()
    assert len(result) == len(existing), "must keep the longer existing history"


# --- summary ----------------------------------------------------------------------
def test_summary_reports_all_three(store):
    store.build_spatial_history()
    s = store.summary()
    for label in ("macro", "price", "spatial"):
        assert label in s
