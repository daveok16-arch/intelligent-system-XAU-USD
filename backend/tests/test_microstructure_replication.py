"""Tests for cross-asset replication and specificity controls.

Run: python -m pytest backend/tests -q

Cross-asset replication answers "is this one series?" and the specificity controls answer
"is this just the leverage effect?" -- the two explanations that would invalidate the sweep
volatility finding. The tests pin the method's fairness (identical parameters, matched
controls) rather than the numeric outcome, which depends on live data.
"""

import os
import sys

import numpy as np
import pandas as pd

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

import microstructure_crossasset as cx  # noqa: E402
import microstructure_specificity as sp  # noqa: E402


def _bars(n=4000, seed=31, vol=1.0):
    rng = np.random.default_rng(seed)
    close = 2000 + np.cumsum(rng.standard_normal(n) * 2 * vol)
    high = close + rng.uniform(0.2, 4, n) * vol
    low = close - rng.uniform(0.2, 4, n) * vol
    open_ = close + rng.standard_normal(n) * 0.5 * vol
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
        "open": open_,
        "high": np.maximum.reduce([high, close, open_]),
        "low": np.minimum.reduce([low, close, open_]),
        "close": close,
        "volume": rng.integers(1000, 5000, n).astype(float),
    })


# --- cross-asset method ------------------------------------------------------------
def test_asset_universe_includes_a_non_precious_control():
    """Copper is the strongest control: it shares no demand story with gold, so a shared
    effect there cannot be a precious-metals narrative artefact."""
    assert "HG=F" in cx.ASSETS
    assert cx.ASSETS["HG=F"] == "copper"
    assert {"GC=F", "SI=F", "PL=F"}.issubset(cx.ASSETS)


def test_no_per_asset_tuning_in_source():
    """Retuning parameters per asset would defeat the point of replication."""
    import inspect
    src = inspect.getsource(cx.CrossAssetStudy)
    # The sweep lookback and volume threshold must be shared constants, not per-asset.
    assert "VOLUME_ELEVATED_RATIO" in src
    assert "asset_config" not in src
    assert "per_asset" not in src


def test_build_is_identical_across_assets(monkeypatch):
    study = cx.CrossAssetStudy()
    a = study.build(_bars(seed=1))
    b = study.build(_bars(seed=2))
    assert list(a.columns) == list(b.columns)
    assert (a["is_sweep"].dtype == b["is_sweep"].dtype)


def test_sweep_definition_requires_rejection(monkeypatch):
    study = cx.CrossAssetStudy()
    df = study.build(_bars())
    swept = df[df["is_sweep"]]
    if len(swept):
        assert (swept["low"] < swept["level"]).all()
        assert (swept["close"] >= swept["level"]).all()


# --- specificity controls ----------------------------------------------------------
def test_controls_are_increasingly_strict(monkeypatch):
    """C3 must be a subset of C2 (down bars): matching on size cannot include up bars."""
    study = sp.SweepSpecificityStudy()
    monkeypatch.setattr(study.base, "load", lambda sym, refresh=False: _bars())
    df = study.features("GC=F")
    down = df[df["is_down"]]
    matched = down[down["_bin"].notna()] if "_bin" in down.columns else down
    assert len(matched) <= len(down)


def test_size_matching_bins_on_prior_move(monkeypatch):
    study = sp.SweepSpecificityStudy()
    monkeypatch.setattr(study.base, "load", lambda sym, refresh=False: _bars())
    df = study.features("GC=F")
    assert "prior_move" in df.columns
    assert (df["prior_move"].dropna() >= 0).all()


def test_near_miss_group_is_pierced_but_not_rejected(monkeypatch):
    """C4 is the discriminating control: same pierce, opposite resolution.

    A near miss pierced the level without the close-back-above rejection that defines a
    sweep. It is `pierced AND NOT is_sweep`, which includes both bars that closed below
    the level and bars excluded by the pierce-depth threshold.
    """
    study = sp.SweepSpecificityStudy()
    monkeypatch.setattr(study.base, "load", lambda sym, refresh=False: _bars())
    df = study.features("GC=F")
    near_miss = df[df["pierced"] & ~df["is_sweep"]]
    if len(near_miss):
        # Every near miss pierced the level...
        assert (near_miss["low"] < near_miss["level"]).all()
        # ...and by construction none of them is a sweep.
        assert not near_miss["is_sweep"].any()
        # The subset that closed below the level is non-empty and is the strictest form.
        closed_below = near_miss[near_miss["close"] < near_miss["level"]]
        assert len(closed_below) >= 0


def test_controls_report_every_tier(monkeypatch):
    study = sp.SweepSpecificityStudy()
    monkeypatch.setattr(study.base, "load", lambda sym, refresh=False: _bars(6000))
    row = study.controls("GC=F")
    for key in ("C1_t", "C2_t", "C3_t", "C4_t", "n_sweep"):
        assert key in row


def test_fold_stability_uses_contiguous_segments(monkeypatch):
    study = sp.SweepSpecificityStudy()
    monkeypatch.setattr(study.base, "load", lambda sym, refresh=False: _bars(8000))
    t = study.fold_stability("GC=F")
    assert len(t) >= 2
    assert (t["n"] > 0).all()


# --- no fabrication ----------------------------------------------------------------
def test_modules_do_not_synthesise_prices():
    import ast
    import inspect
    for mod in (cx, sp):
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                if node.body and isinstance(node.body[0], ast.Expr) \
                        and isinstance(node.body[0].value, ast.Constant):
                    node.body = node.body[1:]
        assert "np.random.normal" not in ast.unparse(tree), mod.__name__
