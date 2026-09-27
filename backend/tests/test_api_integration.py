"""Integration tests for the v1 API surface (Directive 06).

Run: python -m unittest backend/tests/test_api_integration.py
     python -m pytest backend/tests -q

Corrections vs the directive's draft test:
  1. The draft renamed the REAL live repository on disk to .tmp and restored it after.
     A crash mid-test would have left production data missing. Isolation is now done by
     monkeypatching the path, so on-disk state is never touched.
  2. The draft asserted the CORS preflight returns `access-control-allow-origin: *`
     while configuring allow_origins=["*"] WITH allow_credentials=True. Starlette echoes
     the request origin in that combination, so the draft's assertion fails against its
     own config. The app now drops credentials, and this test pins the corrected behavior.
  3. The draft imported `from backend.app.main import app, BASE_DIR`; the real module is
     backend/main.py.
"""

import os
import sys
import unittest

import pandas as pd
from fastapi.testclient import TestClient

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

import main  # noqa: E402


def _macro_repo(path, rows=None):
    rows = rows or [
        {"week_ending_date": "2026-09-18", "fedwatch_dovish_prob": 71.9, "SDI": 0.55,
         "MACRO_GATE": "OPEN", "spot_price": 2603.25, "liquidity_sweep_floor": 2578.0, "source": "CFTC_COT"},
        # Earlier date last in file order: selection must not be positional.
        {"week_ending_date": "2026-09-11", "fedwatch_dovish_prob": 68.2, "SDI": 0.41,
         "MACRO_GATE": "CLOSED", "spot_price": 2570.4, "liquidity_sweep_floor": 2578.0, "source": "SEED"},
    ]
    pd.DataFrame(rows).to_csv(path, index=False)


def _spatial_repo(path):
    pd.DataFrame([
        {"Date": "2026-09-24", "Three_Day_High": 4422.1, "Three_Day_Low": 4310.7,
         "Sweep_Floor": 4309.2, "ATR_14": 104.87, "Source": "YFINANCE_GC=F"},
        {"Date": "2026-09-25", "Three_Day_High": 4414.1, "Three_Day_Low": 4278.3,
         "Sweep_Floor": 4276.8, "ATR_14": 98.86, "Source": "YFINANCE_GC=F"},
    ]).to_csv(path, index=False)


class TestAPIIntegration(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    # --- 503 behaviour, isolated without touching real files ----------------------
    def test_macro_state_503_when_repository_missing(self):
        original = main.MACRO_REPO
        main.MACRO_REPO = "/nonexistent/macro.csv"
        try:
            resp = self.client.get("/api/v1/macro-state")
            self.assertEqual(resp.status_code, 503)
            self.assertIn("initializing", resp.json()["detail"].lower())
        finally:
            main.MACRO_REPO = original

    def test_spatial_503_when_repository_missing(self):
        original = main.SPATIAL_REPO
        main.SPATIAL_REPO = "/nonexistent/spatial.csv"
        try:
            self.assertEqual(self.client.get("/api/v1/spatial-boundaries").status_code, 503)
        finally:
            main.SPATIAL_REPO = original

    def test_empty_repository_returns_503(self):
        import tempfile
        original = main.MACRO_REPO
        with tempfile.TemporaryDirectory() as tmp:
            empty = os.path.join(tmp, "empty.csv")
            pd.DataFrame(
                columns=["week_ending_date", "fedwatch_dovish_prob", "SDI", "MACRO_GATE",
                         "spot_price", "liquidity_sweep_floor", "source"]
            ).to_csv(empty, index=False)
            main.MACRO_REPO = empty
            try:
                self.assertEqual(self.client.get("/api/v1/macro-state").status_code, 503)
            finally:
                main.MACRO_REPO = original

    # --- corrected CORS behaviour -------------------------------------------------
    def test_cors_preflight_allows_wildcard_when_credentials_off(self):
        resp = self.client.options(
            "/api/v1/macro-state",
            headers={
                "Origin": "http://localhost:8501",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Requested-With",
            },
        )
        self.assertEqual(resp.headers.get("access-control-allow-origin"), "*")
        # Credentials must NOT be advertised alongside a wildcard origin.
        self.assertIsNone(resp.headers.get("access-control-allow-credentials"))

    # --- contract shape -----------------------------------------------------------
    def test_macro_state_contract(self):
        import tempfile
        original = main.MACRO_REPO
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "macro.csv")
            _macro_repo(path)
            main.MACRO_REPO = path
            try:
                body = self.client.get("/api/v1/macro-state").json()
            finally:
                main.MACRO_REPO = original
        self.assertEqual(set(body), {"timestamp", "fedwatch_dovish_probability",
                                     "sentiment_divergence_index", "system_gate_status"})
        self.assertEqual(body["timestamp"], "2026-09-18")  # max date, not file order
        self.assertIsInstance(body["fedwatch_dovish_probability"], float)
        self.assertIsInstance(body["sentiment_divergence_index"], float)
        self.assertIn(body["system_gate_status"], {"OPEN", "CLOSED"})

    def test_spatial_boundaries_contract(self):
        import tempfile
        original = main.SPATIAL_REPO
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spatial.csv")
            _spatial_repo(path)
            main.SPATIAL_REPO = path
            try:
                body = self.client.get("/api/v1/spatial-boundaries").json()
            finally:
                main.SPATIAL_REPO = original
        self.assertEqual(set(body), {"date", "three_day_high", "three_day_low",
                                     "sweep_floor", "atr_14", "source"})
        self.assertEqual(body["date"], "2026-09-25")  # max date
        self.assertAlmostEqual(body["sweep_floor"], body["three_day_low"] - 1.50, places=2)

    def test_malformed_rows_are_purged_not_served(self):
        import tempfile
        original = main.MACRO_REPO
        rows = [
            {"week_ending_date": "2026-09-18", "fedwatch_dovish_prob": 71.9, "SDI": 0.55,
             "MACRO_GATE": "OPEN", "spot_price": 2603.25, "liquidity_sweep_floor": 2578.0, "source": "CFTC_COT"},
            # Latest date but corrupted: must be purged, not returned as null JSON.
            {"week_ending_date": "2026-09-25", "fedwatch_dovish_prob": None, "SDI": None,
             "MACRO_GATE": None, "spot_price": None, "liquidity_sweep_floor": None, "source": "BROKEN"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "macro.csv")
            _macro_repo(path, rows)
            main.MACRO_REPO = path
            try:
                body = self.client.get("/api/v1/macro-state").json()
            finally:
                main.MACRO_REPO = original
        self.assertEqual(body["timestamp"], "2026-09-18")
        self.assertIsNotNone(body["sentiment_divergence_index"])


if __name__ == "__main__":
    unittest.main()
