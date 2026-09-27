"""Contract tests for the spatial boundary mapping engine.

Run: python -m pytest backend/tests/test_engine_spatial.py -q

Network is stubbed with frames shaped exactly like yfinance's MultiIndex payload,
so real computation and validation logic is exercised without hitting the feed.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

import engine_spatial as es  # noqa: E402


def _frame(n=40, base=4000.0, seed=7):
    """Build a realistic OHLC frame with Date/High/Low/Close/Open/Volume."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end="2026-09-25", periods=n)
    close = base + np.cumsum(rng.standard_normal(n) * 12)
    high = close + rng.uniform(5, 60, n)
    low = close - rng.uniform(5, 60, n)
    return pd.DataFrame(
        {"Date": dates, "Open": close, "High": high, "Low": low, "Close": close,
         "Volume": rng.integers(100000, 200000, n)}
    )


@pytest.fixture
def engine(tmp_path):
    return es.SpatialBoundaryEngine(data_dir=str(tmp_path))


# --- core math -------------------------------------------------------------------
def test_sweep_floor_is_three_day_low_minus_15_pips(engine):
    out = engine.calculate_boundaries(_frame(), "YFINANCE_GC=F")
    assert np.allclose(out["Sweep_Floor"], out["Three_Day_Low"] - 1.50)


def test_swing_bounds_use_shifted_window_no_lookahead(engine):
    df = _frame()
    out = engine.calculate_boundaries(df, "YFINANCE_GC=F")
    ordered = df.sort_values("Date").reset_index(drop=True)
    row = out.iloc[0]  # most recent
    idx = ordered.index[ordered["Date"] == pd.Timestamp(row["Date"])][0]
    expected_high = ordered["High"].iloc[idx - 3:idx].max()
    expected_low = ordered["Low"].iloc[idx - 3:idx].min()
    assert row["Three_Day_High"] == pytest.approx(expected_high)
    assert row["Three_Day_Low"] == pytest.approx(expected_low)


def test_atr_is_real_not_fabricated_constant(engine):
    """Regression: the draft stamped ATR=10.0 via fillna on early rows.

    Guard must catch ANY fabricated constant, not just an all-constant column,
    and the row count must reflect real-lookback exclusion rather than backfill.
    """
    df = _frame(n=40)
    out = engine.calculate_boundaries(df, "YFINANCE_GC=F")
    assert out["ATR_14"].notna().all()
    assert not (out["ATR_14"] == 10.0).any()  # any fabricated sentinel fails
    # Rows before full lookback (max(3-day swing, 14-period ATR) => index 14) are excluded.
    assert len(out) == len(df) - 14


def test_atr_matches_manual_true_range_mean(engine):
    df = _frame().sort_values("Date").reset_index(drop=True)
    out = engine.calculate_boundaries(df, "YFINANCE_GC=F")
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift(1)).abs()
    lc = (df["Low"] - df["Close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    manual = tr.shift(1).rolling(14).mean()
    row = out.iloc[0]
    idx = df.index[df["Date"] == pd.Timestamp(row["Date"])][0]
    assert row["ATR_14"] == pytest.approx(manual.iloc[idx])


# --- contract: schema & types -----------------------------------------------------
def test_date_is_iso8601_string(engine):
    out = engine.calculate_boundaries(_frame(), "YFINANCE_GC=F")
    assert out["Date"].map(type).eq(str).all()
    assert out["Date"].str.match(r"^\d{4}-\d{2}-\d{2}$").all()


def test_output_columns_match_declared_contract(engine):
    out = engine.calculate_boundaries(_frame(), "YFINANCE_GC=F")
    assert list(out.columns) == ["Date", "Three_Day_High", "Three_Day_Low", "Sweep_Floor", "ATR_14", "Source"]


def test_source_label_propagates(engine):
    out = engine.calculate_boundaries(_frame(), "CACHE_GC=F")
    assert set(out["Source"]) == {"CACHE_GC=F"}


def test_rows_sorted_descending(engine):
    out = engine.calculate_boundaries(_frame(), "YFINANCE_GC=F")
    assert list(out["Date"]) == sorted(out["Date"], reverse=True)


# --- failure behaviour ------------------------------------------------------------
def test_null_ohlc_raises_typed_valueerror(engine):
    df = _frame()
    df.loc[5, "High"] = np.nan
    with pytest.raises(ValueError, match="null"):
        engine._validate_ohlc(df)


def test_insufficient_history_raises(engine):
    with pytest.raises(ValueError, match="insufficient history"):
        engine.calculate_boundaries(_frame(n=8), "YFINANCE_GC=F")


def test_fallback_without_cache_raises_instead_of_fabricating(engine, monkeypatch):
    """Regression: the draft invented constant OHLC prices and wrote them to disk."""
    def boom(*_a, **_k):
        raise RuntimeError("network down")
    monkeypatch.setattr(es.yf, "download", boom)
    with pytest.raises(es.SpatialDataUnavailable):
        engine.fetch_market_history(retries=2, backoff=0)
    assert not os.path.exists(engine.cache_path)  # nothing fabricated on disk


def test_fallback_serves_real_cache_and_labels_it(engine, monkeypatch):
    real = _frame()
    engine._write_cache(real)

    def boom(*_a, **_k):
        raise RuntimeError("network down")
    monkeypatch.setattr(es.yf, "download", boom)

    df, source = engine.fetch_market_history(retries=2, backoff=0)
    assert source == "CACHE_GC=F"
    assert len(df) == len(real)
    assert np.allclose(df["Close"], real["Close"])  # exact real values, not synthesised


def test_multiindex_payload_is_flattened(engine, monkeypatch):
    raw = _frame()
    raw.columns = pd.MultiIndex.from_product([raw.columns, ["GC=F"]])
    monkeypatch.setattr(es.yf, "download", lambda *a, **k: raw)
    df, source = engine.fetch_market_history(retries=1, backoff=0)
    assert "Close" in df.columns
    assert source == "YFINANCE_GC=F"


def test_execute_pipeline_writes_repository(engine, monkeypatch):
    monkeypatch.setattr(es.yf, "download", lambda *a, **k: _frame())
    out = engine.execute_pipeline()
    assert os.path.exists(engine.output_path)
    assert len(out) > 0
    assert set(out["Source"]) == {"YFINANCE_GC=F"}
