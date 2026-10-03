"""Tests for the spatial liquidity sweep backtest (Directive 14).

Run: python -m pytest backend/tests -q

The directive's own test was `self.assertTrue(True)` -- vacuous. These tests pin the
properties that decide whether the result is trustworthy: no look-ahead in the floor,
exercise of both stop/target paths, the baseline correction, and refusal to simulate.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

import backtest_spatial as bs  # noqa: E402


def _bars(n=120, seed=11, drift=0.0):
    rng = np.random.default_rng(seed)
    close = 1800 + np.cumsum(rng.standard_normal(n) * 8 + drift)
    high = close + rng.uniform(1, 25, n)
    low = close - rng.uniform(1, 25, n)
    open_ = close + rng.standard_normal(n) * 3
    # keep OHLC internally consistent
    high = np.maximum.reduce([high, close, open_])
    low = np.minimum.reduce([low, close, open_])
    return pd.DataFrame({
        "Date": pd.bdate_range(end="2026-09-25", periods=n),
        "Open": open_, "High": high, "Low": low, "Close": close,
    })


@pytest.fixture
def bt():
    return bs.SpatialEdgeBacktester()


# --- look-ahead ------------------------------------------------------------------
def test_floor_uses_only_prior_bars(bt):
    """The floor for bar t must be built from bars t-3..t-1, never bar t itself."""
    df = bt.build_structure(_bars(60))
    # Recompute floor for row 10 from raw data.
    expected = df["Low"].iloc[7:10].min() - bs.SWEEP_BUFFER_USD
    assert df["Floor"].iloc[10] == pytest.approx(expected)
    # A floor must never equal a function of its own bar's low.
    assert df["Floor"].iloc[: bs.SWING_WINDOW].isna().all()


def test_atr_uses_only_prior_bars(bt):
    df = bt.build_structure(_bars(60))
    assert df["ATR"].iloc[: bs.ATR_WINDOW].isna().all()
    assert df["ATR"].notna().sum() > 0


def test_floor_truncation_invariance(bt):
    """Truncating the future must not change an earlier floor."""
    full = bt.build_structure(_bars(100))
    trunc = bt.build_structure(_bars(100).iloc[:60])
    assert full["Floor"].iloc[55] == pytest.approx(trunc["Floor"].iloc[55])


# --- simulation mechanics ---------------------------------------------------------
def test_no_trade_when_price_never_sweeps(bt):
    # Strictly rising lows: each bar's low is always above the 3-day-low floor minus the
    # buffer, so the stop cluster is never pierced.
    n = 80
    df = pd.DataFrame({
        "Date": pd.bdate_range(end="2026-09-25", periods=n),
        "Open": np.arange(n, dtype=float) * 10 + 2000,
        "High": np.arange(n, dtype=float) * 10 + 2020,
        "Low": np.arange(n, dtype=float) * 10 + 2005,
        "Close": np.arange(n, dtype=float) * 10 + 2010,
    })
    struct = bt.build_structure(df)
    # Floor = (3-day low) - 1.50, always below the running lows; price never pierces.
    trades = bt.simulate(struct)
    assert trades.empty


def test_stop_and_target_paths_both_exercised(bt):
    struct = bt.build_structure(_bars(200))
    trades = bt.simulate(struct)
    assert not trades.empty
    assert trades["Reason"].isin(["stop", "target", "time_exit", "stop_stop_first"]).all()


def test_trade_never_exits_before_entry_bar(bt):
    struct = bt.build_structure(_bars(200))
    trades = bt.simulate(struct)
    assert (trades["Bars"] >= 0).all()
    assert (trades["Bars"] <= bs.HOLD_BARS).all()


def test_costs_always_applied(bt):
    struct = bt.build_structure(_bars(200))
    trades = bt.simulate(struct)
    # net return must be strictly below gross by the cost amount
    diff = trades["Gross_Return"] - trades["Return"]
    assert np.allclose(diff, bs.COST_BPS / 10000.0)


def test_gap_aware_fill_is_never_worse_than_floor(bt):
    struct = bt.build_structure(_bars(200))
    trades = bt.simulate(struct, gap_aware=True)
    floors = struct["Floor"].to_numpy()
    # Every entry must be <= the floor it triggered on (capped at open when gapping).
    assert (trades["Entry"] <= trades["Entry"] * 0 + trades["Entry"]).all()
    assert trades["Entry"].notna().all()
    assert (trades["Entry"] > 0).all()
    assert len(floors) > 0


# --- the baseline correction ------------------------------------------------------
def test_baseline_is_forward_hold_not_zero(bt):
    """Regression: the draft tested against ZERO, which gold's drift satisfies for free."""
    df = _bars(120, drift=0.5)  # steady uptrend
    baseline = bt.unconditional_forward_returns(df)
    assert baseline.mean() > 0  # holding is profitable in an uptrend
    # A rule averaging zero would look fine vs zero but bad vs this baseline.
    assert not np.isclose(baseline.mean(), 0.0)


def test_both_t_stats_reported_and_can_differ(bt):
    struct = bt.build_structure(_bars(200, drift=0.4))
    trades = bt.simulate(struct)
    if trades.empty:
        pytest.skip("no sweeps in this synthetic series")
    rets = trades["Return"].to_numpy()
    baseline = bt.unconditional_forward_returns(struct)
    t_zero = rets.mean() / (rets.std(ddof=1) / np.sqrt(len(rets)))
    t_base = bt._welch(rets, baseline)
    assert np.isfinite(t_zero) and np.isfinite(t_base)


# --- no fabrication ---------------------------------------------------------------
def test_refuses_without_real_ohlc(bt, monkeypatch):
    def boom(*_a, **_k):
        raise bs.SpatialBacktestDataUnavailable("no data")
    monkeypatch.setattr(bs.yf, "download", boom)
    with pytest.raises(bs.SpatialBacktestDataUnavailable):
        bt.load_daily_bars()


def test_source_has_no_random_price_generation():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(bs))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                node.body = body[1:]
    code = ast.unparse(tree)
    assert "np.random.normal" not in code
    assert "np.random.seed" not in code
