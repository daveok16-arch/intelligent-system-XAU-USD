"""Tests for the unified strategy optimizer (Directive 15).

Run: python -m pytest backend/tests -q

The directive's own test only asserted the return value is not None. These tests pin the
two defects that produced spectacular-but-false results in the draft:
  1. synthesising prices from the indicator being traded (circular), and
  2. crediting exits on the entry bar (perfect intrabar foresight).
"""

import ast
import inspect
import os
import sys

import numpy as np
import pandas as pd
import pytest

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

import backtest_spatial as bs  # noqa: E402
import strategy_optimizer as so  # noqa: E402


def _bars(n=150, seed=5):
    rng = np.random.default_rng(seed)
    close = 1900 + np.cumsum(rng.standard_normal(n) * 9)
    high = close + rng.uniform(2, 30, n)
    low = close - rng.uniform(2, 30, n)
    open_ = close + rng.standard_normal(n) * 3
    high = np.maximum.reduce([high, close, open_])
    low = np.minimum.reduce([low, close, open_])
    return pd.DataFrame({
        "Date": pd.bdate_range(end="2026-09-25", periods=n),
        "Open": open_, "High": high, "Low": low, "Close": close,
    })


@pytest.fixture
def engine():
    return bs.SpatialEdgeBacktester()


# --- defect 1: circular synthetic prices ------------------------------------------
def test_optimizer_never_synthesises_prices_from_indicators():
    """Regression: the draft built Close/Low/High out of Sweep_Floor and ATR_14, then
    backtested the sweep strategy against those invented prices. The traded indicator
    must never be the source of the price series."""
    tree = ast.parse(inspect.getsource(so))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) \
                    and isinstance(node.body[0].value, ast.Constant):
                node.body = node.body[1:]
    code = ast.unparse(tree)
    assert "Sweep_Floor'] +" not in code
    assert "Sweep_Floor\"] +" not in code
    assert "ATR_14" not in code, "optimizer must not reference structural columns as prices"


def test_no_raw_bars_fallback_to_invented_prices():
    """The draft's missing-file fallback invented prices. Inspect executable code only."""
    tree = ast.parse(inspect.getsource(so))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) \
                    and isinstance(node.body[0].value, ast.Constant):
                node.body = node.body[1:]
    code = ast.unparse(tree)
    assert "gold_historical_daily_raw" not in code, \
        "the draft's missing-file fallback invented prices; it must not exist"


# --- defect 2: entry-bar foresight -------------------------------------------------
def test_entry_bar_exit_is_off_by_default(engine):
    sig = inspect.signature(engine.simulate)
    assert sig.parameters["allow_entry_bar_exit"].default is False


def test_foresight_inflates_results_versus_honest(engine):
    """The whole apparent edge came from resolving the target on the entry bar. With
    foresight off, the same cell must not look better."""
    bars = engine.build_structure(_bars(300))
    with_f = engine.simulate(bars, atr_target_mult=0.5, atr_stop_mult=2.0,
                             hold_bars=1, allow_entry_bar_exit=True)
    without_f = engine.simulate(bars, atr_target_mult=0.5, atr_stop_mult=2.0,
                                hold_bars=1, allow_entry_bar_exit=False)
    if with_f.empty or without_f.empty:
        pytest.skip("no sweeps in synthetic series")
    assert with_f["Return"].mean() >= without_f["Return"].mean()


def test_no_exit_resolved_on_entry_bar_when_disabled(engine):
    """With foresight off, a trade must never exit at bar 0 via a target/stop."""
    bars = engine.build_structure(_bars(300))
    trades = engine.simulate(bars, hold_bars=3, allow_entry_bar_exit=False)
    if trades.empty:
        pytest.skip("no sweeps")
    entry_bar_exits = trades[(trades["Bars"] == 0) & (trades["Reason"].isin(["target", "stop"]))]
    assert entry_bar_exits.empty


# --- convention-free predictive test ----------------------------------------------
def test_predictive_test_is_convention_free(engine):
    """Both sides must use the same entry convention (close), so the result cannot be
    driven by the strategy entering cheaper than the baseline."""
    bars = engine.build_structure(_bars(400))
    out = engine.sweep_predictive_test(bars)
    assert set(out.columns) == {"Horizon_Bars", "Sweep_Mean_Fwd_Pct",
                                "Unconditional_Mean_Fwd_Pct", "Edge_Pct", "T_Stat"}
    assert len(out) == 4
    for _, row in out.iterrows():
        assert row["Edge_Pct"] == pytest.approx(
            row["Sweep_Mean_Fwd_Pct"] - row["Unconditional_Mean_Fwd_Pct"], abs=1e-3)


def test_predictive_test_detects_no_edge_on_random_walk(engine):
    """On a pure random walk the sweep event should show no significant edge."""
    bars = engine.build_structure(_bars(600, seed=17))
    out = engine.sweep_predictive_test(bars)
    assert out["T_Stat"].abs().max() < 3.0


# --- optimizer plumbing ------------------------------------------------------------
def test_grid_returns_expected_columns(engine, monkeypatch):
    opt = so.IntegratedStrategyOptimizer()
    monkeypatch.setattr(opt, "engine", engine)
    monkeypatch.setattr(opt, "load_bars_with_regime", lambda: engine.build_structure(_bars(300)))
    monkeypatch.setattr(so, "TARGET_MULTS", [1.0])
    monkeypatch.setattr(so, "STOP_MULTS", [1.0])
    monkeypatch.setattr(so, "HOLD_WINDOWS", [3])
    monkeypatch.setattr(so, "MIN_TRADES", 5)
    grid = opt.run_combinatorial_sweep()
    assert isinstance(grid, pd.DataFrame)
    if not grid.empty:
        for col in ("Target_Mult", "Stop_Mult", "Hold_Bars", "Trade_Count",
                    "True_T_Stat_vs_Baseline"):
            assert col in grid.columns


def test_macro_half_skipped_when_no_macro_history(engine, monkeypatch):
    """Absent macro history, the gated half must be skipped rather than faking a gate."""
    opt = so.IntegratedStrategyOptimizer()
    bars = engine.build_structure(_bars(300))
    bars["MACRO_GATE"] = None
    bars["SDI"] = np.nan
    monkeypatch.setattr(opt, "load_bars_with_regime", lambda: bars)
    monkeypatch.setattr(opt, "engine", engine)
    monkeypatch.setattr(so, "TARGET_MULTS", [1.0])
    monkeypatch.setattr(so, "STOP_MULTS", [1.0])
    monkeypatch.setattr(so, "HOLD_WINDOWS", [3])
    monkeypatch.setattr(so, "MIN_TRADES", 5)
    grid = opt.run_combinatorial_sweep()
    if not grid.empty:
        assert not grid["Macro_Gated"].any()


# --- unittest compatibility -------------------------------------------------------
# The directive's verification command is `python -m unittest backend/tests/test_strategy_optimizer.py`,
# which collects only TestCase classes. These wrappers ensure that command reports real
# results instead of "NO TESTS RAN".
import unittest  # noqa: E402

from backtest_spatial import SpatialEdgeBacktester as _Engine  # noqa: E402


class TestStrategyOptimizer(unittest.TestCase):
    def setUp(self):
        self.opt = so.IntegratedStrategyOptimizer()

    def test_tensor_bounds_processing(self):
        """The directive's named test: the sweep must return a valid frame."""
        bars = _Engine().build_structure(_bars(300))
        self.opt.load_bars_with_regime = lambda: bars
        so.TARGET_MULTS, so.STOP_MULTS, so.HOLD_WINDOWS = [1.0], [1.0], [3]
        so.MIN_TRADES = 5
        grid = self.opt.run_combinatorial_sweep()
        self.assertIsNotNone(grid)
        self.assertIsInstance(grid, pd.DataFrame)

    def test_entry_bar_foresight_defaults_off(self):
        """Regression: crediting an exit on the entry bar flips the sign of the result."""
        sig = inspect.signature(_Engine().simulate)
        self.assertFalse(sig.parameters["allow_entry_bar_exit"].default)

    def test_optimizer_has_no_synthetic_price_fallback(self):
        tree = ast.parse(inspect.getsource(so))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                if node.body and isinstance(node.body[0], ast.Expr) \
                        and isinstance(node.body[0].value, ast.Constant):
                    node.body = node.body[1:]
        self.assertNotIn("gold_historical_daily_raw", ast.unparse(tree))
