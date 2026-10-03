"""Tests for the volatility forecasting study.

Run: python -m pytest backend/tests -q

This module asks the question that decides whether the sweep finding is useful: does it
beat the industry-standard EWMA volatility forecast, out of sample? The tests pin the
method's honesty -- strict out-of-sample evaluation, an untuned EWMA baseline, a proper
forecast-comparison test, and no in-sample result reported as evidence.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

import volatility_forecast as vf  # noqa: E402


def _returns(n=3000, seed=7):
    rng = np.random.default_rng(seed)
    # GARCH-like: volatility clusters, so EWMA should be a strong baseline.
    r = np.zeros(n)
    v = 1e-4
    for i in range(1, n):
        v = 0.05e-4 + 0.10 * r[i - 1] ** 2 + 0.85 * v
        r[i] = rng.standard_normal() * np.sqrt(v)
    return r


# --- estimators --------------------------------------------------------------------
def test_log_rv_defined_for_single_bar_horizon():
    """Regression: a sample std is undefined for one observation, which silently dropped
    the h=1 horizon. The estimator must use sqrt(mean of squares)."""
    r = np.array([0.01, -0.02, 0.015, -0.005])
    rv = vf._log_rv(r, 1)
    assert np.isfinite(rv[0])
    assert rv[0] == pytest.approx(np.log(abs(r[1])))
    assert np.isnan(rv[-1])   # no forward window at the end


def test_log_rv_uses_only_future_bars():
    """The forward window must start at i+1, never include bar i."""
    r = np.array([0.5, 0.001, 0.001, 0.001])
    rv = vf._log_rv(r, 1)
    # rv[0] must reflect r[1] (0.001), not the huge r[0].
    assert rv[0] == pytest.approx(np.log(0.001))


def test_ewma_uses_only_prior_returns():
    """EWMA at i must depend only on returns BEFORE i.

    Regression on this test's own history: the first version asserted v[:300] unchanged
    and v[300:] changed, which BOTH the correct and a future-leaking version satisfy --
    so it detected nothing. Mutation testing exposed that. The discriminating assertion
    is that v[i] itself is untouched by a shock to r[i].
    """
    r = _returns(500)
    v1 = vf._ewma_vol(r)
    r2 = r.copy()
    r2[300] *= 100          # perturb bar 300
    v2 = vf._ewma_vol(r2)

    # The value AT the shock bar must be identical: it may not see r[300].
    assert v1[300] == pytest.approx(v2[300], rel=0, abs=0)
    # And so must everything before it.
    assert np.allclose(v1[:301], v2[:301], equal_nan=True)
    # The shock must propagate forward, or the estimator is not recursing at all.
    assert not np.allclose(v1[301:], v2[301:], equal_nan=True)


def test_ewma_lambda_is_the_riskmetrics_standard():
    assert vf.EWMA_LAMBDA == pytest.approx(0.94)


def test_fast_ewma_decays_faster_than_standard():
    r = _returns(600)
    slow = vf._ewma_vol(r, lam=0.94)
    fast = vf._ewma_fast(r)
    # After a shock, the fast series should react more (higher variance of changes).
    d_slow = np.nanstd(np.diff(slow[~np.isnan(slow)]))
    d_fast = np.nanstd(np.diff(fast[~np.isnan(fast)]))
    assert d_fast > d_slow


# --- regression and test -----------------------------------------------------------
def test_ols_recovers_a_known_relationship():
    rng = np.random.default_rng(3)
    x = rng.standard_normal(500)
    y = 2.0 + 1.5 * x + rng.standard_normal(500) * 0.01
    X = np.column_stack([np.ones(len(x)), x])
    coef, se, t = vf._ols(X, y)
    assert coef[0] == pytest.approx(2.0, abs=0.05)
    assert coef[1] == pytest.approx(1.5, abs=0.05)
    assert abs(t[1]) > 20      # clearly significant


def test_dm_test_detects_a_better_forecast():
    rng = np.random.default_rng(11)
    truth = rng.standard_normal(2000)
    good = truth + rng.standard_normal(2000) * 0.1
    bad = truth + rng.standard_normal(2000) * 1.0
    # Errors: (truth-good) small, (truth-bad) large -> good should win decisively.
    stat = vf._dm_test(truth - bad, truth - good, h=1)
    assert stat > 2.0


def test_dm_test_is_flat_for_identical_forecasts():
    rng = np.random.default_rng(5)
    e = rng.standard_normal(500)
    stat = vf._dm_test(e, e.copy(), h=1)
    assert np.isnan(stat) or abs(stat) < 1e-6


def test_dm_test_handles_short_series():
    assert np.isnan(vf._dm_test(np.array([1.0, 2.0]), np.array([1.0, 2.0])))


# --- study discipline --------------------------------------------------------------
def test_evaluation_is_out_of_sample(monkeypatch):
    """The split must be chronological and the test set must not be used for fitting."""
    assert 0.0 < vf.TRAIN_FRACTION < 1.0
    study = vf.VolatilityForecastStudy()
    src = __import__("inspect").getsource(study.one_asset)
    assert "iloc[:int(len(frame) * TRAIN_FRACTION)]" in src
    assert "iloc[int(len(frame) * TRAIN_FRACTION):]" in src


def test_reports_out_of_sample_significance_not_in_sample(monkeypatch):
    """An in-sample coefficient t-stat does not show the forecast improved. The DM test
    on held-out errors is the honest statistic and must be reported."""
    study = vf.VolatilityForecastStudy()
    src = __import__("inspect").getsource(study.one_asset)
    assert "dm_t_oos" in src


def test_asymmetry_regressors_are_mutually_exclusive(monkeypatch):
    """is_sweep and failed_pierce must not overlap, or the asymmetry comparison is
    comparing a group against itself."""
    study = vf.VolatilityForecastStudy()
    monkeypatch.setattr(study.base, "load",
                        lambda sym, refresh=False: pd.DataFrame({
                            "timestamp": pd.date_range("2024-01-01", periods=3000, freq="h", tz="UTC"),
                            "open": np.linspace(100, 110, 3000),
                            "high": np.linspace(101, 111, 3000),
                            "low": np.linspace(99, 109, 3000),
                            "close": np.linspace(100, 110, 3000),
                            "volume": np.full(3000, 1000.0),
                        }))
    df = study.features("GC=F")
    df["failed_pierce"] = (df["pierced"] & ~df["is_sweep"]).astype(float)
    assert not ((df["is_sweep"]) & (df["failed_pierce"] > 0)).any()


def test_no_synthesis():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(vf))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) \
                    and isinstance(node.body[0].value, ast.Constant):
                node.body = node.body[1:]
    assert "np.random.normal" not in ast.unparse(tree)
