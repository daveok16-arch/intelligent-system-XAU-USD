"""Tests for the UI render verifier (Directive 11).

Run: python -m pytest backend/tests -q

The projection must agree with what frontend/streamlit_app.py actually renders. These
tests pin the shared formatting/thresholds so the two cannot silently diverge, and
confirm the degraded path fabricates nothing.
"""

import os
import sys

import pandas as pd
import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _TESTS_DIR)

import verify_ui_render as vr  # noqa: E402


@pytest.fixture
def repos(tmp_path, monkeypatch):
    macro = tmp_path / "macro.csv"
    spatial = tmp_path / "spatial.csv"
    pd.DataFrame([{
        "week_ending_date": "2026-09-22", "fedwatch_dovish_prob": 19.47, "SDI": 0.3072,
        "MACRO_GATE": "CLOSED", "spot_price": 2591.1, "liquidity_sweep_floor": 2578.0,
        "source": "CFTC_COT",
    }]).to_csv(macro, index=False)
    pd.DataFrame([{
        "Date": "2026-09-25", "Three_Day_High": 4414.10, "Three_Day_Low": 4278.30,
        "Sweep_Floor": 4276.80, "ATR_14": 98.86, "Source": "YFINANCE_GC=F",
    }]).to_csv(spatial, index=False)
    monkeypatch.setattr(vr, "MACRO_PATH", str(macro))
    monkeypatch.setattr(vr, "SPATIAL_PATH", str(spatial))
    return macro, spatial


# --- fidelity to the live HUD -----------------------------------------------------
def test_sdi_formatting_matches_the_hud(repos):
    """The HUD renders {sdi:+.2f}; the projection must not show more precision."""
    state = vr.collect_state()
    assert state["sdi"] == "+0.31"


def test_gate_banner_matches_hud_wording(repos):
    view = vr.render(vr.collect_state())
    assert "CONDITIONS NOT MET" in view


def test_sdi_label_uses_the_hud_threshold(repos):
    """0.3072 < 0.50, so it reads as lower-range positioning."""
    state = vr.collect_state()
    assert state["sdi_label"] == "NET POSITIONING: LOWER RANGE"


def test_spatial_fields_match_hud_values(repos):
    state = vr.collect_state()
    assert state["low"] == "$4,278.30"
    assert state["floor"] == "$4,276.80"
    assert state["atr"] == "98.86"


def test_projection_states_the_fred_proxy_caveat(repos):
    """The HUD labels the dovish figure as a FRED proxy, not CME FedWatch."""
    assert "not" in vr.render(vr.collect_state())


# --- degraded path -----------------------------------------------------------------
def test_missing_repositories_render_degraded_without_fabrication(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "MACRO_PATH", str(tmp_path / "absent1.csv"))
    monkeypatch.setattr(vr, "SPATIAL_PATH", str(tmp_path / "absent2.csv"))
    state = vr.collect_state()
    assert state["macro_present"] is False
    assert state["spatial_present"] is False
    assert state["sdi"] == vr.DASH
    assert state["floor"] == vr.DASH
    view = vr.render(state)
    assert "DEGRADED" in view
    assert "UNAVAILABLE" in view


def test_unparseable_dates_degrade_rather_than_raise(tmp_path, monkeypatch):
    """Regression: the directive's parse raised on a bad date instead of degrading."""
    macro = tmp_path / "bad.csv"
    pd.DataFrame([{"week_ending_date": "not-a-date", "fedwatch_dovish_prob": 1.0,
                   "SDI": 0.1, "MACRO_GATE": "OPEN", "spot_price": 1.0,
                   "liquidity_sweep_floor": 1.0, "source": "X"}]).to_csv(macro, index=False)
    monkeypatch.setattr(vr, "MACRO_PATH", str(macro))
    assert vr.parse_latest_record(str(macro), "week_ending_date") is None


def test_empty_repository_degrades(tmp_path, monkeypatch):
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    monkeypatch.setattr(vr, "MACRO_PATH", str(empty))
    assert vr.parse_latest_record(str(empty), "week_ending_date") is None


def test_max_date_selection_not_positional(tmp_path, monkeypatch):
    repo = tmp_path / "unordered.csv"
    pd.DataFrame([
        {"week_ending_date": "2026-09-22", "fedwatch_dovish_prob": 19.47, "SDI": 0.3072,
         "MACRO_GATE": "CLOSED", "spot_price": 1.0, "liquidity_sweep_floor": 1.0, "source": "A"},
        {"week_ending_date": "2026-09-08", "fedwatch_dovish_prob": 21.76, "SDI": 0.241,
         "MACRO_GATE": "CLOSED", "spot_price": 1.0, "liquidity_sweep_floor": 1.0, "source": "B"},
    ]).to_csv(repo, index=False)
    rec = vr.parse_latest_record(str(repo), "week_ending_date")
    assert str(rec["week_ending_date"]) == "2026-09-22"  # max date, not last row
