"""Tests for the vectorized backtest engine (Directive 13).

Run: python -m pytest backend/tests -q

The directive's own test was `self.assertTrue(True)` -- vacuous, it asserted nothing.
These tests pin the properties that actually matter: no look-ahead, real data required,
honest metrics, and refusal instead of simulation.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

import backtest_engine as be  # noqa: E402


def _synthetic(n=200, seed=3):
    """Deterministic weekly frame shaped like load_history() output."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(end="2026-09-25", periods=n, freq="W-FRI")
    return pd.DataFrame({
        "Target_Date": dates,
        "Close": 1500 + np.cumsum(rng.standard_normal(n) * 10),
        "Commercial_Net": -250000 + np.cumsum(rng.standard_normal(n) * 3000),
        "open_interest_all": 400000.0,
        "policy_rate": 2.0,
        "two_year": 2.0 + rng.standard_normal(n) * 0.1,
    })


@pytest.fixture
def bt(tmp_path):
    return be.VectorizedBacktester(data_dir=str(tmp_path))


# --- look-ahead prevention --------------------------------------------------------
def test_execution_signal_is_shifted_one_bar(bt):
    df = bt.build_signals(_synthetic())
    # The execution column at index i must equal the signal at index i-1.
    for i in range(1, len(df)):
        assert df["Execution_Signal"].iloc[i] == df["Signal"].iloc[i - 1]
    assert df["Execution_Signal"].iloc[0] == 0.0


def test_sdi_uses_rolling_window_not_full_sample(bt):
    """Regression: a full-sample min/max percentile uses the future to normalise the
    past -- look-ahead bias. Each week must be ranked only against its trailing window."""
    df = _synthetic(n=150)
    out = bt.build_signals(df)
    # The first SDI_WINDOW-1 rows cannot be ranked yet.
    assert out["SDI"].iloc[: be.SDI_WINDOW - 1].isna().all()
    # A row's SDI must depend only on data up to that row: truncating the tail must
    # not change earlier values.
    truncated = bt.build_signals(df.iloc[:120])
    mid = 100
    assert out["SDI"].iloc[mid] == pytest.approx(truncated["SDI"].iloc[mid])


def test_gate_requires_both_conditions(bt):
    df = _synthetic()
    out = bt.build_signals(df)
    opened = out[out["MACRO_GATE"] == "OPEN"]
    if len(opened):
        assert (opened["SDI"] > 0.50).all()
        assert (opened["dovish"] > 0.50).all()


# --- no fabrication ---------------------------------------------------------------
def test_refuses_when_price_history_is_unavailable(bt, monkeypatch):
    """Regression: the directive fell back to np.random.normal(0.0005, 0.01) and reported
    a Sharpe ratio computed from random numbers."""
    def boom(*_a, **_k):
        raise be.BacktestDataUnavailable("no prices")
    monkeypatch.setattr(be.yf, "download", boom)
    with pytest.raises(be.BacktestDataUnavailable):
        bt.load_history()


def test_engine_source_contains_no_random_fallback():
    """No executable path may synthesise returns. Docstrings/comments describing the
    removed fallback are fine; actual code is not -- so inspect the AST, not the text."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(be))
    # Drop docstrings so prose describing the defect is not mistaken for code.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:]
    code = ast.unparse(tree)
    assert "np.random.normal" not in code
    assert "np.random.seed" not in code


# --- metrics ----------------------------------------------------------------------
def test_metrics_are_internally_consistent(bt):
    out = bt.build_signals(_synthetic())
    m = bt._metrics(out["Strategy_Returns"])
    assert m["max_drawdown_pct"] <= 0
    assert np.isfinite(m["annualized_sharpe"])
    assert m["final_value"] > 0


def test_costs_reduce_returns_vs_cost_free(bt, monkeypatch):
    df = _synthetic()
    with_costs = bt.build_signals(df)
    monkeypatch.setattr(be, "COST_BPS_PER_TRADE", 0.0)
    without_costs = bt.build_signals(df)
    assert with_costs["Strategy_Returns"].sum() <= without_costs["Strategy_Returns"].sum()


def test_flat_strategy_when_gate_never_opens(bt, monkeypatch):
    """If the gate never opens, exposure and PnL must both be exactly zero."""
    df = _synthetic()
    # Force dovish to be perpetually low so the gate cannot open.
    df["two_year"] = df["policy_rate"] + 5.0
    out = bt.build_signals(df)
    assert (out["MACRO_GATE"] == "CLOSED").all()
    assert out["Execution_Signal"].sum() == 0.0
    assert out["Strategy_Returns"].sum() == 0.0


def test_selection_t_stat_reported(bt):
    metrics = bt.execute_historical_analysis(_synthetic(n=300))
    assert "Selection_Edge_t_stat" in metrics
    assert metrics["Benchmark_BuyHold_Return_Pct"] is not None


def test_insufficient_history_is_rejected(bt):
    with pytest.raises(ValueError):
        bt.build_signals(pd.DataFrame({"Target_Date": [], "Close": [], "Commercial_Net": [],
                                       "policy_rate": [], "two_year": []}))
