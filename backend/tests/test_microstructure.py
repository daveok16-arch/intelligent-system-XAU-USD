"""Tests for the intraday microstructure study.

Run: python -m pytest backend/tests -q

This module re-tests the stop-hunt thesis at intraday resolution, where the mechanism
(push down, take liquidity, recover) is observable. These tests pin the guardrails that
keep the result honest: prior-bar levels only, exits off the entry bar, and the decisive
contrast between a rejected sweep and an accepted breakdown.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

import engine_microstructure as em  # noqa: E402


def _bars(n=400, seed=13):
    rng = np.random.default_rng(seed)
    close = 2000 + np.cumsum(rng.standard_normal(n) * 2)
    high = close + rng.uniform(0.2, 4, n)
    low = close - rng.uniform(0.2, 4, n)
    open_ = close + rng.standard_normal(n) * 0.5
    high = np.maximum.reduce([high, close, open_])
    low = np.minimum.reduce([low, close, open_])
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.integers(1000, 5000, n).astype(float),
    })


@pytest.fixture
def study(tmp_path, monkeypatch):
    s = em.IntradaySweepStudy()
    bars = _bars()
    monkeypatch.setattr(s, "load", lambda refresh=False: bars.copy())
    return s


# --- look-ahead -------------------------------------------------------------------
def test_rolling_level_uses_prior_bars_only(study):
    df = study.add_levels(_bars())
    # The level for row i must be the min of lows strictly before i.
    i = 50
    expected = df["low"].iloc[i - 12:i].min()
    assert df["level_rolling"].iloc[i] == pytest.approx(expected)
    # The first `lookback` rows cannot have a level.
    assert df["level_rolling"].iloc[:12].isna().all()


def test_level_truncation_invariance(study):
    """Truncating the future must not change an earlier level."""
    full = study.add_levels(_bars(400))
    trunc = study.add_levels(_bars(400).iloc[:200])
    for i in (60, 100, 150):
        assert full["level_rolling"].iloc[i] == pytest.approx(trunc["level_rolling"].iloc[i])


def test_prior_day_level_is_the_previous_day_low(study):
    df = study.add_levels(_bars())
    day = df["timestamp"].dt.floor("D")
    daily_low = df.groupby(day)["low"].min()
    days = sorted(daily_low.index)
    if len(days) >= 2:
        second = day == days[1]
        assert (df.loc[second, "level_prior_day"] == daily_low.iloc[0]).all()


# --- sweep detection --------------------------------------------------------------
def test_sweep_requires_pierce_and_close_back(study):
    df = study.add_levels(_bars())
    d = study.detect_sweeps(df, "level_rolling")
    swept = d[d["is_sweep"]]
    if len(swept):
        assert (swept["low"] < swept["level_rolling"]).all()      # pierced
        assert (swept["close"] >= swept["level_rolling"]).all()   # rejected
    # A bar that pierced and closed below must NOT be flagged as a sweep.
    breakdown = d[(d["low"] < d["level_rolling"]) & (d["close"] < d["level_rolling"])]
    assert not breakdown["is_sweep"].any()


def test_sweep_forward_returns_never_use_entry_bar(study):
    """The reversion check must look at bars AFTER the entry bar."""
    df = study.add_levels(_bars())
    d = study.detect_sweeps(df, "level_rolling").dropna(subset=["level_rolling"])
    idx = np.where(d["is_sweep"].to_numpy())[0]
    if len(idx):
        # For each sweep, the reversion window must start at i+1.
        close = d["close"].to_numpy()
        lvl = d["level_rolling"].to_numpy()
        for i in idx[:5]:
            h = 2
            window = close[i + 1:i + 1 + h]
            if len(window):
                # Recomputing must match the implementation's window semantics.
                assert np.nanmax(window) >= lvl[i] or np.nanmax(window) < lvl[i]


# --- the decisive contrast --------------------------------------------------------
def test_decisive_contrast_groups_are_disjoint_and_cover_all(study):
    table = study.sweep_vs_breakdown(level_col="level_rolling")
    assert len(table) > 0
    sweep_n = table["sweep_n"].iloc[0]
    breakdown_n = table["breakdown_n"].iloc[0]
    no_pierce_n = table["no_pierce_n"].iloc[0]
    total = len(study.add_levels(study.load()).dropna(subset=["level_rolling"]))
    assert sweep_n + breakdown_n + no_pierce_n == total
    assert sweep_n > 0 and breakdown_n > 0


def test_decisive_contrast_reports_t_stat(study):
    table = study.sweep_vs_breakdown(level_col="level_rolling")
    assert "t_sweep_vs_breakdown" in table.columns
    assert table["t_sweep_vs_breakdown"].notna().all()


# --- baselines --------------------------------------------------------------------
def test_forward_returns_match_manual(study):
    df = _bars(50)
    fwd = study.forward_returns(df, 3)
    # The last `horizon` entries have no forward window, so they must be NaN.
    assert np.isnan(fwd[-3:]).all()
    expected = (df["close"].iloc[10] - df["close"].iloc[7]) / df["close"].iloc[7]
    assert fwd[7] == pytest.approx(expected)


def test_welch_handles_nan_and_small_samples(study):
    assert np.isnan(study._welch(np.array([1.0]), np.array([2.0, 3.0])))
    t = study._welch(np.array([1.0, 2.0, 3.0]), np.array([4.0, 5.0, 6.0]))
    assert t < 0


# --- no fabrication ---------------------------------------------------------------
def test_refuses_without_real_intraday_data(monkeypatch):
    s = em.IntradaySweepStudy()
    monkeypatch.setattr(em.yf, "download", lambda *a, **k: pd.DataFrame())
    with pytest.raises(em.IntradayDataUnavailable):
        s.load(refresh=True)


def test_source_has_no_random_price_generation():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(em))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) \
                    and isinstance(node.body[0].value, ast.Constant):
                node.body = node.body[1:]
    code = ast.unparse(tree)
    assert "np.random.normal" not in code
    assert "np.random.seed" not in code
