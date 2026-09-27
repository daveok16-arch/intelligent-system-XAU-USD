"""Tests for production chronological scheduling and API hardening (Directive 09).

Run: python -m unittest backend/tests/test_production_scheduler.py
     python -m pytest backend/tests -q

The directive's draft test was a stub: `with pytz.utc:` raises TypeError (UTC is not a
context manager) and `assertFalse(False)` passes vacuously, so it verified nothing.
These tests instead assert real scheduling behavior with an injected clock.
"""

import os
import sys
import unittest
from datetime import datetime, timezone

import pandas as pd
import pytz

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

from orchestrator import should_trigger_weekly_macro, should_trigger_daily_spatial  # noqa: E402

EST = pytz.timezone("US/Eastern")
UTC = pytz.utc


def _write_macro(path, week_ending_date):
    pd.DataFrame([{
        "week_ending_date": week_ending_date, "fedwatch_dovish_prob": 50.0, "SDI": 0.5,
        "MACRO_GATE": "OPEN", "spot_price": 2600.0, "liquidity_sweep_floor": 2585.0, "source": "TEST",
    }]).to_csv(path, index=False)


def _write_spatial(path, date):
    pd.DataFrame([{
        "Date": date, "Three_Day_High": 4400.0, "Three_Day_Low": 4280.0,
        "Sweep_Floor": 4278.5, "ATR_14": 100.0, "Source": "TEST",
    }]).to_csv(path, index=False)


class TestWeeklyMacroScheduler(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "macro.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def test_blocks_before_friday_release_window(self):
        """Friday 15:00 EST is before the 15:30 release: must not trigger."""
        friday_1500 = EST.localize(datetime(2026, 9, 25, 15, 0))
        self.assertFalse(should_trigger_weekly_macro(self.repo, now=friday_1500))

    def test_triggers_after_friday_release_window(self):
        """Friday 16:00 EST, no prior data for the week: must trigger."""
        friday_1600 = EST.localize(datetime(2026, 9, 25, 16, 0))
        self.assertTrue(should_trigger_weekly_macro(self.repo, now=friday_1600))

    def test_blocks_midweek(self):
        """Thursday: tape not released yet."""
        thursday = EST.localize(datetime(2026, 9, 24, 12, 0))
        self.assertFalse(should_trigger_weekly_macro(self.repo, now=thursday))

    def test_blocks_when_week_already_synced(self):
        """Same ISO week already on disk: do not re-ingest."""
        _write_macro(self.repo, "2026-09-25")
        friday_1600 = EST.localize(datetime(2026, 9, 25, 16, 0))
        self.assertFalse(should_trigger_weekly_macro(self.repo, now=friday_1600))

    def test_triggers_for_new_week(self):
        """Following week: prior row is a different ISO week, so trigger."""
        _write_macro(self.repo, "2026-09-25")
        next_friday = EST.localize(datetime(2026, 10, 2, 16, 0))
        self.assertTrue(should_trigger_weekly_macro(self.repo, now=next_friday))

    def test_clock_anomaly_blocks_execution(self):
        """Fail-safe: an unparseable clock/timezone must block, not run."""
        self.assertFalse(should_trigger_weekly_macro(self.repo, now=None, clock_ok=False))

    def test_naive_now_is_treated_as_utc(self):
        """A naive datetime must not raise; it is assumed UTC."""
        # 2026-09-25 21:00 UTC == 17:00 EST Friday -> after the release.
        self.assertTrue(should_trigger_weekly_macro(self.repo, now=datetime(2026, 9, 25, 21, 0)))

    def test_corrupt_repository_triggers_refresh(self):
        with open(self.repo, "w") as fh:
            fh.write("not,a,valid,repository\n")
        friday_1600 = EST.localize(datetime(2026, 9, 25, 16, 0))
        self.assertTrue(should_trigger_weekly_macro(self.repo, now=friday_1600))


class TestDailySpatialScheduler(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "spatial.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def test_triggers_when_repo_missing(self):
        self.assertTrue(should_trigger_daily_spatial(self.repo, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc)))

    def test_skips_when_current_day_cached(self):
        """Daily-roll guard: today's row present means no network scrape."""
        _write_spatial(self.repo, "2026-09-25")
        now = datetime(2026, 9, 25, 23, 59, tzinfo=timezone.utc)
        self.assertFalse(should_trigger_daily_spatial(self.repo, now=now))

    def test_triggers_on_new_day(self):
        _write_spatial(self.repo, "2026-09-24")
        now = datetime(2026, 9, 25, 0, 1, tzinfo=timezone.utc)
        self.assertTrue(should_trigger_daily_spatial(self.repo, now=now))

    def test_clock_anomaly_blocks_execution(self):
        _write_spatial(self.repo, "2026-09-24")
        self.assertFalse(should_trigger_daily_spatial(self.repo, now=None, clock_ok=False))


if __name__ == "__main__":
    unittest.main()
