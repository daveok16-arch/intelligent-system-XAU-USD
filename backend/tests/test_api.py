"""Contract tests for the backend API surface.

Run: python -m pytest backend/tests -q
"""

import os
import sys

import pandas as pd
import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

os.environ.setdefault("SYSTEM_AUTH_TOKEN", "test-token-abc123")
AUTH_HEADERS = {"Authorization": "Bearer test-token-abc123"}

import main  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    repo = tmp_path / "repo.csv"
    pd.DataFrame(
        [
            {"week_ending_date": "2026-09-11", "fedwatch_dovish_prob": 68.2, "SDI": 0.41,
             "MACRO_GATE": "CLOSED", "spot_price": 2570.4, "liquidity_sweep_floor": 2578.0, "source": "SEED"},
            {"week_ending_date": "2026-09-25", "fedwatch_dovish_prob": 74.5, "SDI": 0.68,
             "MACRO_GATE": "OPEN", "spot_price": 2591.1, "liquidity_sweep_floor": 2578.0, "source": "CFTC_COT"},
            # Earlier date placed LAST in file order to prove selection isn't positional.
            {"week_ending_date": "2026-09-18", "fedwatch_dovish_prob": 71.9, "SDI": 0.55,
             "MACRO_GATE": "OPEN", "spot_price": 2603.25, "liquidity_sweep_floor": 2578.0, "source": "SEED"},
        ]
    ).to_csv(repo, index=False)

    monkeypatch.setattr(main, "MACRO_REPO", str(repo))
    monkeypatch.setattr(main.market_data, "get_spot", lambda: {"price": 4286.2, "source": "gold-api.com", "as_of": "T"})
    with TestClient(main.app, headers=AUTH_HEADERS) as c:
        yield c


def test_state_selects_by_max_date_not_file_order(client):
    body = client.get("/api/state").json()
    assert body["timestamp"] == "2026-09-25"
    assert body["data_source"] == "CFTC_COT"


def test_state_exposes_spatial_and_market_fields(client):
    body = client.get("/api/state").json()
    assert body["spot_price"] == pytest.approx(2591.1)
    assert body["liquidity_sweep_floor"] == pytest.approx(2578.0)
    assert body["distance_to_floor_pct"] == pytest.approx(0.508, abs=1e-3)
    assert body["market_spot"] == pytest.approx(4286.2)


def test_force_gate_override_removed(client):
    """Directive 09: the gate-flip test hook must no longer exist.

    A query param that can silently alter a decision surface is a hazard; passing it
    must be ignored (and must never change the reported gate).
    """
    baseline = client.get("/api/state").json()["system_gate_status"]
    tampered = client.get("/api/state?force_gate=CLOSED").json()["system_gate_status"]
    assert tampered == baseline


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_missing_repository_returns_503(client, monkeypatch):
    monkeypatch.setattr(main, "MACRO_REPO", "/nonexistent/repo.csv")
    assert client.get("/api/state").status_code == 503
