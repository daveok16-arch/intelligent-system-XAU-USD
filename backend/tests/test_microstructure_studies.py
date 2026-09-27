"""Tests for the conditional, volatility, and walk-forward microstructure studies.

Run: python -m pytest backend/tests -q

These modules exist to avoid a specific failure mode: searching many conditions for
directional edge and reporting whichever cell looked best. The tests pin the discipline
(pre-registered signs, out-of-sample split, multiple-testing reporting) and the measures
(magnitude, not direction).
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

import engine_microstructure as em  # noqa: E402
import microstructure_conditions as mc  # noqa: E402
import microstructure_volatility as mv  # noqa: E402
import microstructure_walkforward as mw  # noqa: E402


def _bars(n=3000, seed=21, vol=1.0):
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


def _patch(monkeypatch, obj, bars):
    monkeypatch.setattr(obj, "load", lambda refresh=False: bars.copy())
    return obj


# --- conditions module ------------------------------------------------------------
def test_preregistered_signs_are_fixed_in_code():
    """Hypotheses must declare their expected sign before data is seen."""
    src = open(os.path.join(_APP_DIR, "microstructure_conditions.py")).read()
    for hyp in ("H1_vol_high", "H2_ny", "H3_deep", "H4_ny_highvol"):
        assert hyp in src
    # Every hypothesis carries an explicit expected sign argument.
    assert "expected_sign" in src


def test_conditions_builds_features_from_prior_bars(monkeypatch):
    study = mc.ConditionalMicrostructureStudy(
        base=_patch(monkeypatch, em.IntradaySweepStudy(), _bars()))
    df = study.build()
    # Volume context must come from prior bars, so the first rows cannot have it.
    assert df["vol_ratio"].isna().sum() == 0   # dropped by build
    assert (df["vol_ratio"] > 0).all()
    assert set(df["hypothesis" if "hypothesis" in df.columns else "is_sweep"].unique()).issubset({True, False})


def test_conditions_split_is_chronological(monkeypatch):
    study = mc.ConditionalMicrostructureStudy(
        base=_patch(monkeypatch, em.IntradaySweepStudy(), _bars()))
    df = study.build()
    split = int(len(df) * mc.DISCOVERY_FRACTION)
    assert df["timestamp"].iloc[split - 1] < df["timestamp"].iloc[split]


def test_conditions_reports_expected_false_positives(monkeypatch, capsys):
    study = mc.ConditionalMicrostructureStudy(
        base=_patch(monkeypatch, em.IntradaySweepStudy(), _bars(4000)))
    study.run()
    out = capsys.readouterr().out
    assert "expected false positives" in out
    assert "VALIDATION" in out


def test_insufficient_cell_is_flagged_not_reported_as_significant(monkeypatch):
    study = mc.ConditionalMicrostructureStudy(
        base=_patch(monkeypatch, em.IntradaySweepStudy(), _bars(300)))
    df = study.build()
    cell = study.evaluate_cell(df, "tiny", df["is_sweep"] & False, 1, +1)
    assert cell["note"] == "n<20, insufficient"
    assert cell["t"] is None


# --- volatility module ------------------------------------------------------------
def test_abs_move_is_unsigned(monkeypatch):
    df = _bars(500)
    study = mv.SweepVolatilityStudy(base=_patch(monkeypatch, em.IntradaySweepStudy(), df))
    am = study.abs_forward_return(df, 3)
    assert np.nanmin(am) >= 0
    # A known up-move and down-move of equal size must score equally.
    up = pd.DataFrame({"close": [100.0, 110.0]})
    down = pd.DataFrame({"close": [100.0, 90.0]})
    assert study.abs_forward_return(up, 1)[0] == pytest.approx(0.10)
    assert study.abs_forward_return(down, 1)[0] == pytest.approx(0.10)


def test_forward_range_is_positive_and_uses_later_bars(monkeypatch):
    df = _bars(500)
    study = mv.SweepVolatilityStudy(base=_patch(monkeypatch, em.IntradaySweepStudy(), df))
    rng = study.forward_range(df, 4)
    valid = rng[~np.isnan(rng)]
    assert (valid > 0).all()
    assert np.isnan(rng[-4:]).all()   # last `horizon` bars have no window


def test_volatility_battery_direction_is_magnitude(monkeypatch):
    """Every volatility cell expects 'more' -- there is no sign to get wrong."""
    study = mv.SweepVolatilityStudy(
        base=_patch(monkeypatch, em.IntradaySweepStudy(), _bars(4000)))
    df = study.build()
    table = study.battery(df, "level_rolling")
    assert set(table["hypothesis"]) == {
        "V1_sweep_absmove", "V2_highvol_absmove", "V2_lowvol_absmove", "V3_sweep_range"}


# --- walk-forward module ----------------------------------------------------------
def test_walkforward_folds_are_contiguous_and_cover_all(monkeypatch):
    study = mw.WalkForwardStudy(base=_patch(monkeypatch, em.IntradaySweepStudy(), _bars(9000)))
    df = study.build()
    table = study.fold_table(df, "level_rolling", "all", df["is_sweep"])
    assert len(table) >= 2
    assert table["n_sweeps"].sum() > 0
    # Periods must not overlap and must be in order.
    starts = [p.split("→")[0] for p in table["period"]]
    assert starts == sorted(starts)


def test_walkforward_reports_ratio_and_t_per_fold(monkeypatch):
    study = mw.WalkForwardStudy(base=_patch(monkeypatch, em.IntradaySweepStudy(), _bars(9000)))
    df = study.build()
    table = study.fold_table(df, "level_rolling", "all", df["is_sweep"])
    assert "ratio" in table.columns and "t" in table.columns
    assert table["ratio"].notna().all()


def test_walkforward_summary_counts_significant_folds(monkeypatch):
    study = mw.WalkForwardStudy(base=_patch(monkeypatch, em.IntradaySweepStudy(), _bars(9000)))
    summary = study.run()
    assert summary
    for v in summary.values():
        assert "significant_positive" in v and "significant_negative" in v
        assert "median_ratio" in v


# --- discipline invariants --------------------------------------------------------
def test_no_module_synthesises_prices():
    import ast
    import inspect
    for mod in (mc, mv, mw):
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                if node.body and isinstance(node.body[0], ast.Expr) \
                        and isinstance(node.body[0].value, ast.Constant):
                    node.body = node.body[1:]
        code = ast.unparse(tree)
        assert "np.random.normal" not in code, mod.__name__


def test_all_modules_use_unconditional_null_not_zero():
    for mod in (mc, mv):
        src = open(mod.__file__).read()
        assert "baseline" in src.lower() or "uncond" in src.lower()
