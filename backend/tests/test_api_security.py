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


class TestBruteForceAndRotation(unittest.TestCase):
    """Rate limiting on repeated 401s, and token rotation without client downtime."""

    def setUp(self):
        os.environ["SYSTEM_AUTH_TOKEN"] = TOKEN
        os.environ["AUTH_FAILURE_MAX"] = "3"
        os.environ["AUTH_FAILURE_WINDOW_SECONDS"] = "60"
        importlib.reload(main)          # picks up AUTH_FAILURE_MAX
        main._failed_auth.clear()
        self.client = TestClient(main.app)

    def tearDown(self):
        for k in ("SYSTEM_AUTH_TOKEN", "AUTH_FAILURE_MAX", "AUTH_FAILURE_WINDOW_SECONDS",
                  "SYSTEM_AUTH_TOKEN_PREVIOUS"):
            os.environ.pop(k, None)
        importlib.reload(main)

    def test_repeated_failures_trigger_429(self):
        """After the threshold, further attempts are rejected without re-checking the token."""
        statuses = [self.client.get("/api/v1/macro-state",
                                    headers={"Authorization": "Bearer wrong"}).status_code
                    for _ in range(6)]
        self.assertIn(401, statuses)
        self.assertEqual(statuses[-1], 429)
        self.assertEqual(statuses.count(429), 3)  # max=3 failures allowed, then limited

    def test_429_carries_retry_after(self):
        for _ in range(4):
            self.client.get("/api/v1/macro-state", headers={"Authorization": "Bearer wrong"})
        resp = self.client.get("/api/v1/macro-state", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.headers.get("retry-after"), "60")

    def test_valid_token_clears_the_failure_counter(self):
        for _ in range(2):
            self.client.get("/api/v1/macro-state", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(self.client.get("/api/v1/macro-state", headers=AUTH).status_code, 200)
        # Counter cleared, so we can fail again without immediately hitting the limit.
        self.assertEqual(
            self.client.get("/api/v1/macro-state", headers={"Authorization": "Bearer wrong"}).status_code,
            401)

    def test_rate_limit_is_per_origin(self):
        for _ in range(4):
            self.client.get("/api/v1/macro-state", headers={"Authorization": "Bearer wrong",
                                                            "X-Forwarded-For": "10.0.0.1"})
        # A different origin must be unaffected.
        other = self.client.get("/api/v1/macro-state", headers={"Authorization": "Bearer wrong",
                                                                "X-Forwarded-For": "10.0.0.2"})
        self.assertEqual(other.status_code, 401)

    def test_health_is_never_rate_limited(self):
        for _ in range(10):
            self.client.get("/health")
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_previous_token_is_accepted_during_rotation(self):
        os.environ["SYSTEM_AUTH_TOKEN_PREVIOUS"] = "old-token-1,old-token-2"
        importlib.reload(main)
        main._failed_auth.clear()
        client = TestClient(main.app)
        for tok in (TOKEN, "old-token-1", "old-token-2"):
            self.assertEqual(client.get("/api/v1/macro-state",
                                        headers={"Authorization": f"Bearer {tok}"}).status_code, 200)
        self.assertEqual(client.get("/api/v1/macro-state",
                                    headers={"Authorization": "Bearer retired"}).status_code, 401)

    def test_accepted_tokens_defaults_to_primary_only(self):
        self.assertEqual(main.accepted_tokens(), [TOKEN])


if __name__ == "__main__":
    unittest.main()
