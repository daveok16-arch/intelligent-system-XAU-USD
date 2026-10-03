"""Tests for the robustness controls.

Run: python -m pytest backend/tests -q

These controls exist because two earlier findings were at risk of being artefacts:
session clustering could be volatility, and the volatility forecast could be volatility
persistence. Both must be tested with the stricter null, not assumed.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

import microstructure_robustness as rb  # noqa: E402


def _bars(n=4000, seed=41):
    rng = np.random.default_rng(seed)
    close = 2000 + np.cumsum(rng.standard_normal(n) * 2)
    high = close + rng.uniform(0.2, 4, n)
    low = close - rng.uniform(0.2, 4, n)
    open_ = close + rng.standard_normal(n) * 0.5
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
        "open": open_,
        "high": np.maximum.reduce([high, close, open_]),
        "low": np.minimum.reduce([low, close, open_]),
        "close": close,
        "volume": rng.integers(1000, 5000, n).astype(float),
    })


@pytest.fixture
def study(monkeypatch):
    s = rb.RobustnessStudy()
    monkeypatch.setattr(s.base, "load", lambda sym, refresh=False: _bars())
    return s


# --- R1 session control ------------------------------------------------------------
def test_session_control_normalises_by_volatility(study):
    """The control must divide pierce rate by mean absolute return, so a session that
    simply moves more cannot look like it has more sweeps."""
    r = study.session_control("GC=F")
    if r is None:
        pytest.skip("synthetic series has no session split")
    assert "vol_normalised_ratio" in r
    assert "raw_ratio" in r
    # The normalised ratio must not equal the raw one (the control is actually applied).
    assert r["vol_normalised_ratio"] != r["raw_ratio"]


def test_session_control_reports_survival_judgement(study):
    r = study.session_control("GC=F")
    if r is None:
        pytest.skip("no session split")
    assert isinstance(r["survives"], bool)


def test_volatility_normalisation_removes_a_pure_vol_artefact(monkeypatch):
    """Construct a series where NY is purely more volatile and has NO extra structure.
    The normalised ratio must be ~1.0, i.e. the control correctly detects the artefact."""
    n = 6000
    rng = np.random.default_rng(2)
    ts = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    ny = ts.hour.to_numpy()
    is_ny = (ny >= 13) & (ny < 19)
    # NY bars move twice as much, but with no structural difference otherwise.
    scale = np.where(is_ny, 2.0, 1.0)
    close = 2000 + np.cumsum(rng.standard_normal(n) * 2 * scale)
    df = pd.DataFrame({
        "timestamp": ts, "open": close, "close": close,
        "high": close + rng.uniform(0.2, 4, n) * scale,
        "low": close - rng.uniform(0.2, 4, n) * scale,
        "volume": np.full(n, 1000.0),
    })
    s = rb.RobustnessStudy()
    monkeypatch.setattr(s.base, "load", lambda sym, refresh=False: df)
    r = s.session_control("GC=F")
    if r is None:
        pytest.skip("no session split")
    # Raw ratio should be > 1 (NY moves more) but the normalised ratio near 1.
    assert r["raw_ratio"] > 1.05
    assert 0.7 < r["vol_normalised_ratio"] < 1.4


# --- R2 forecast control -----------------------------------------------------------
def test_forecast_control_includes_current_volatility(study):
    r = study.forecast_control("GC=F")
    if r is None:
        pytest.skip("insufficient sample")
    assert "rmse_ewma_curvol" in r
    assert "sweep_t_with_curvol" in r
    # Adding current volatility must improve on EWMA alone (volatility persists).
    assert r["rmse_ewma_curvol"] <= r["rmse_ewma"] * 1.001


def test_forecast_control_reports_out_of_sample_dm(study):
    r = study.forecast_control("GC=F")
    if r is None:
        pytest.skip("insufficient sample")
    assert "dm_t_vs_curvol" in r
    assert isinstance(r["survives"], bool)


def test_forecast_survival_requires_out_of_sample_significance(study):
    """A significant in-sample sweep coefficient is NOT sufficient; the control must
    require out-of-sample DM significance."""
    import inspect
    src = inspect.getsource(rb.RobustnessStudy.forecast_control)
    assert "_dm_test" in src
    assert "dm >= 2.0" in src


# --- no fabrication ----------------------------------------------------------------
def test_no_synthesis():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(rb))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) \
                    and isinstance(node.body[0].value, ast.Constant):
                node.body = node.body[1:]
    assert "np.random.normal" not in ast.unparse(tree)
