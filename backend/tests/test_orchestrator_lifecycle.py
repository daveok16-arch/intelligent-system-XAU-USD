"""Tests for the orchestrator lifecycle (Directive 08).

Run: python -m unittest backend/tests/test_orchestrator_lifecycle.py
     python -m pytest backend/tests -q

Extends the directive's three cases with regression guards for the defects that
made the draft unable to boot, and with a real end-to-end lifecycle test.
"""

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

import orchestrator  # noqa: E402
from orchestrator import MasterSystemOrchestrator  # noqa: E402


class TestOrchestratorLifecycle(unittest.TestCase):
    def setUp(self):
        self.orchestrator = MasterSystemOrchestrator()

    # --- directive cases ----------------------------------------------------------
    @patch("requests.get")
    def test_api_health_polling_success(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        self.assertTrue(self.orchestrator.poll_api_health(max_retries=1))

    @patch("requests.get")
    def test_api_health_polling_failure_handling(self, mock_get):
        mock_get.side_effect = Exception("Connection Refused")
        self.assertFalse(self.orchestrator.poll_api_health(max_retries=2))

    def test_shutdown_event_propagation(self):
        self.orchestrator.shutdown_signal.set()
        self.assertTrue(self.orchestrator.shutdown_signal.is_set())

    # --- regression: unbindable health URL ----------------------------------------
    def test_health_url_is_a_resolvable_host(self):
        """Regression: the draft polled 'http://127.0.0', host '127.0.0' port 80."""
        from urllib.parse import urlparse

        parsed = urlparse(orchestrator.HEALTH_URL)
        self.assertEqual(parsed.hostname, "127.0.0.1")
        self.assertNotEqual(parsed.hostname, "127.0.0")
        self.assertIsNotNone(parsed.port)

    def test_invalid_health_host_is_repaired(self):
        """A malformed HEALTH_URL must be normalised rather than used verbatim."""
        with patch.dict(os.environ, {"HEALTH_URL": "http://127.0.0"}):
            import importlib
            importlib.reload(orchestrator)
            try:
                self.assertEqual(orchestrator.HEALTH_URL, "http://127.0.0.1:8000/health")
            finally:
                os.environ.pop("HEALTH_URL", None)
                importlib.reload(orchestrator)

    # --- regression: child dies -> fail fast, no hang -----------------------------
    @patch("requests.get")
    def test_health_poll_fails_fast_when_child_exits(self, mock_get):
        mock_get.side_effect = Exception("refused")
        proc = MagicMock()
        proc.poll.return_value = 1  # child already dead
        self.orchestrator.api_process = proc
        self.assertFalse(self.orchestrator.poll_api_health(max_retries=5))

    def test_shutdown_event_short_circuits_health_poll(self):
        self.orchestrator.shutdown_signal.set()
        self.assertFalse(self.orchestrator.poll_api_health(max_retries=5))

    # --- cache warming ------------------------------------------------------------
    def test_warm_data_caches_triggers_macro_when_missing(self, ):
        events = []

        class FakeIngestor:
            def __init__(self, asset_symbol=None):
                pass

            def ingest_weekly_macro_gravity(self):
                events.append("macro")
                return {}

            def ingest_institutional_fund_flow(self):
                return {}

            def synthesize_sentiment_divergence(self, *_a):
                return {}

        fake_engine = MagicMock()
        fake_engine.InstitutionalDataIngestor = FakeIngestor

        with patch.dict(sys.modules, {"engine_macro": fake_engine}):
            with patch.object(self.orchestrator, "macro_repo", "/nonexistent/macro.csv"):
                with patch.object(self.orchestrator, "spatial_repo", "/nonexistent/spatial.csv"):
                    self.orchestrator.warm_data_caches()
        self.assertIn("macro", events)

    # --- end-to-end lifecycle -----------------------------------------------------
    def test_full_lifecycle_boot_and_ordered_shutdown(self):
        """Boots the real API, verifies health, then tears it down with no survivors."""
        import subprocess

        if not os.path.exists(self.orchestrator.macro_repo):
            self.orchestrator.warm_data_caches()

        orch = MasterSystemOrchestrator()
        orch.api_process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
             "--port", "8123", "--log-level", "warning"],
            cwd=_BACKEND_DIR,
        )
        try:
            self.assertTrue(orch.poll_api_health(url="http://127.0.0.1:8123/health", max_retries=15))
        finally:
            orch.terminate_lifecycle()
        self.assertIsNotNone(orch.api_process.poll())  # no orphaned child


if __name__ == "__main__":
    unittest.main()
