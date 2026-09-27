"""Verification tests for the cockpit's v1 data wiring (Directive 07).

Run: python -m pytest frontend/tests -q

Covers the fallback chain (live v1 -> local repository -> baseline), the two
defects corrected in this directive, and a real render of the app.

Note: importing the frontend module executes the Streamlit script. That is safe
under bare mode (no ScriptRunContext) and is how the loader functions are reached.
"""

import os
import sys

import pandas as pd
import pytest

_FRONTEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _FRONTEND_DIR)

import streamlit_app as app  # noqa: E402


# --- fallback chain ---------------------------------------------------------------
def test_macro_prefers_live_v1(monkeypatch):
    monkeypatch.setattr(app, "_get_json", lambda url, params=None, timeout=4: {
        "timestamp": "2026-09-22",
        "fedwatch_dovish_probability": 14.93,
        "sentiment_divergence_index": 0.5471,
        "system_gate_status": "closed",
    })
    state, source = app.load_macro_state()
    assert source == "LIVE /api/v1/macro-state"
    assert state["system_gate_status"] == "CLOSED"  # normalized to upper
    assert state["sentiment_divergence_index"] == pytest.approx(0.5471)


def test_macro_falls_back_to_repository_when_backend_absent(monkeypatch, tmp_path):
    repo = tmp_path / "macro.csv"
    pd.DataFrame([
        {"week_ending_date": "2026-09-18", "fedwatch_dovish_prob": 71.9, "SDI": 0.55,
         "MACRO_GATE": "OPEN", "spot_price": 2603.25, "liquidity_sweep_floor": 2578.0, "source": "CFTC_COT"},
        # Earlier date last in file: positional selection would pick the wrong row.
        {"week_ending_date": "2026-09-11", "fedwatch_dovish_prob": 68.2, "SDI": 0.41,
         "MACRO_GATE": "CLOSED", "spot_price": 2570.4, "liquidity_sweep_floor": 2578.0, "source": "SEED"},
    ]).to_csv(repo, index=False)

    monkeypatch.setattr(app, "_get_json", lambda url, params=None, timeout=4: None)
    monkeypatch.setattr(app, "MACRO_REPO", str(repo))

    state, source = app.load_macro_state()
    assert source == "LOCAL MACRO REPOSITORY"
    assert state["timestamp"] == "2026-09-18"  # max date, NOT the last file row


def test_macro_baseline_when_nothing_available(monkeypatch):
    monkeypatch.setattr(app, "_get_json", lambda url, params=None, timeout=4: None)
    monkeypatch.setattr(app, "MACRO_REPO", "/nonexistent/macro.csv")
    state, source = app.load_macro_state()
    assert source == "MACRO BASELINE UNAVAILABLE"
    assert state["sentiment_divergence_index"] is None  # never fabricated


def test_spatial_prefers_live_v1(monkeypatch):
    monkeypatch.setattr(app, "_get_json", lambda url, params=None, timeout=4: {
        "date": "2026-09-25", "three_day_high": 4414.10, "three_day_low": 4278.30,
        "sweep_floor": 4276.80, "atr_14": 98.86, "source": "YFINANCE_GC=F",
    })
    spatial, source = app.load_spatial()
    assert source == "LIVE /api/v1/spatial-boundaries"
    assert spatial["sweep_floor"] == pytest.approx(4276.80)


def test_spatial_falls_back_to_repository(monkeypatch, tmp_path):
    repo = tmp_path / "spatial.csv"
    pd.DataFrame([
        {"Date": "2026-09-25", "Three_Day_High": 4414.1, "Three_Day_Low": 4278.3,
         "Sweep_Floor": 4276.8, "ATR_14": 98.86, "Source": "YFINANCE_GC=F"},
        {"Date": "2026-09-24", "Three_Day_High": 4422.1, "Three_Day_Low": 4310.7,
         "Sweep_Floor": 4309.2, "ATR_14": 104.87, "Source": "YFINANCE_GC=F"},
    ]).to_csv(repo, index=False)

    monkeypatch.setattr(app, "_get_json", lambda url, params=None, timeout=4: None)
    monkeypatch.setattr(app, "SPATIAL_REPO", str(repo))

    spatial, source = app.load_spatial()
    assert source == "LOCAL SPATIAL REPOSITORY"
    assert spatial["date"] == "2026-09-25"
    assert spatial["sweep_floor"] == pytest.approx(4276.8)


def test_spatial_baseline_when_nothing_available(monkeypatch):
    monkeypatch.setattr(app, "_get_json", lambda url, params=None, timeout=4: None)
    monkeypatch.setattr(app, "SPATIAL_REPO", "/nonexistent/spatial.csv")
    spatial, source = app.load_spatial()
    assert source == "SPATIAL BASELINE UNAVAILABLE"
    assert spatial["sweep_floor"] is None


def test_malformed_live_payload_falls_through_to_repository(monkeypatch, tmp_path):
    """A live payload missing fields must not crash; it falls through."""
    repo = tmp_path / "spatial.csv"
    pd.DataFrame([{"Date": "2026-09-25", "Three_Day_High": 4414.1, "Three_Day_Low": 4278.3,
                   "Sweep_Floor": 4276.8, "ATR_14": 98.86, "Source": "YFINANCE_GC=F"}]).to_csv(repo, index=False)

    monkeypatch.setattr(app, "_get_json", lambda url, params=None, timeout=4: {"date": "2026-09-25"})  # incomplete
    monkeypatch.setattr(app, "SPATIAL_REPO", str(repo))
    spatial, source = app.load_spatial()
    assert source == "LOCAL SPATIAL REPOSITORY"


# --- defect regressions -----------------------------------------------------------
def test_sweep_buffer_is_150_pips_not_15():
    """Directive 04/07 naming: $1.50 on gold is 150 pips at $0.01/pip."""
    assert app.SWEEP_BUFFER_USD == 1.50
    # The rendered label must not claim 15 pips.
    import inspect
    src = inspect.getsource(app)
    assert "150 pips" in src
    assert "(15 pips)" not in src


# --- real render ------------------------------------------------------------------
def test_app_renders_without_exception(monkeypatch):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(os.path.join(_FRONTEND_DIR, "streamlit_app.py"), default_timeout=45).run()
    assert len(at.exception) == 0, [e.value for e in at.exception]
    md = " ".join(m.value for m in at.markdown)
    captions = " ".join(c.value for c in at.caption)
    # Spatial card must now be driven by the spatial source, not the macro repo.
    assert "SPATIAL MARKET BOUNDARY" in md
    assert "Spatial source:" in captions
    assert "150 pips" in md
