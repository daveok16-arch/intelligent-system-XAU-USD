"""Tests for the bearer authentication layer (Directive 10).

Run: python -m unittest backend/tests/test_api_security.py
     python -m pytest backend/tests -q

Beyond the directive's cases, this suite pins the two fixes that matter:
  - /api/state serves the SAME payload as /api/v1/macro-state, so protecting only the
    v1 routes would have been a cosmetic bypass. Every /api/* path is protected.
  - a missing SYSTEM_AUTH_TOKEN must fail closed, not fall back to a hardcoded secret.
"""

import importlib
import os
import sys
import unittest

from fastapi.testclient import TestClient

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

import main  # noqa: E402

TOKEN = "test-token-abc123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class TestAPISecurity(unittest.TestCase):
    def setUp(self):
        os.environ["SYSTEM_AUTH_TOKEN"] = TOKEN
        importlib.reload(main)
        self.client = TestClient(main.app)

    def tearDown(self):
        os.environ.pop("SYSTEM_AUTH_TOKEN", None)
        importlib.reload(main)

    # --- directive cases ----------------------------------------------------------
    def test_protected_routes_enforce_401_without_token(self):
        for endpoint in ["/api/v1/macro-state", "/api/v1/spatial-boundaries", "/api/history"]:
            self.assertEqual(self.client.get(endpoint).status_code, 401, endpoint)

    def test_health_check_remains_open_unauthenticated(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    # --- the bypass the directive would have left open ----------------------------
    def test_legacy_state_route_is_also_protected(self):
        """Regression: /api/state returns the same macro payload, so it must not be open."""
        self.assertEqual(self.client.get("/api/state").status_code, 401)

    def test_all_api_routes_require_auth_by_default(self):
        """Every /api/* path is protected, so a new route cannot be open by omission."""
        for route in main.app.routes:
            path = getattr(route, "path", "")
            if path.startswith("/api/") and "{" not in path:
                self.assertEqual(self.client.get(path).status_code, 401, path)

    def test_spot_and_refresh_protected(self):
        self.assertEqual(self.client.get("/api/spot").status_code, 401)
        self.assertEqual(self.client.post("/api/refresh").status_code, 401)

    # --- token handling -----------------------------------------------------------
    def test_valid_token_grants_access(self):
        self.assertEqual(self.client.get("/api/v1/macro-state", headers=AUTH).status_code, 200)

    def test_wrong_token_rejected(self):
        resp = self.client.get("/api/v1/macro-state", headers={"Authorization": "Bearer wrong-token"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.headers.get("www-authenticate"), "Bearer")

    def test_missing_bearer_prefix_rejected(self):
        """A raw token without 'Bearer ' must not authenticate."""
        self.assertEqual(self.client.get("/api/v1/macro-state", headers={"Authorization": TOKEN}).status_code, 401)

    def test_challenge_header_present_on_401(self):
        resp = self.client.get("/api/state")
        self.assertEqual(resp.headers.get("www-authenticate"), "Bearer")

    def test_empty_token_env_fails_closed(self):
        """Regression: the draft fell back to a hardcoded token. Blank env must not
        grant access with any caller-supplied value."""
        os.environ["SYSTEM_AUTH_TOKEN"] = ""
        importlib.reload(main)
        client = TestClient(main.app)
        # The draft's hardcoded literal must NOT be accepted.
        self.assertEqual(
            client.get("/api/state", headers={"Authorization": "Bearer INSTITUTIONAL_CORE_SECURE_TOKEN_2026"}).status_code,
            401,
        )
        # And no arbitrary token works either.
        self.assertEqual(client.get("/api/state", headers={"Authorization": "Bearer anything"}).status_code, 401)

    def test_401_body_does_not_leak_state(self):
        body = self.client.get("/api/v1/macro-state").json()
        self.assertEqual(set(body), {"detail"})
        self.assertNotIn("sentiment_divergence_index", body)


if __name__ == "__main__":
    unittest.main()
