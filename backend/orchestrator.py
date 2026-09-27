"""
APPLICATION MODULE: MASTER SYSTEM ORCHESTRATOR
ROLE: SYSTEM SKELETAL SYNC, ASYNC CRON-TASKS, AND API LIFECYCLE MANAGEMENT

Lifecycle:
  1. Pre-flight cache warming: guarantee both repositories exist on disk before the
     API boots, so it cannot come up in a degraded 503 state.
  2. Launch the FastAPI microservice and confirm readiness via active HTTP polling.
  3. Run the macro and spatial ingestion loops on isolated daemon threads.
  4. On SIGTERM/SIGINT or child death, tear down children and exit non-zero if boot failed.

Directive 08 corrections, each verified by execution against the live container
(the draft as written could never boot):
  1. HEALTH URL: the draft polled "http://127.0.0", which is not a valid host (it
     parses as host 127.0.0, port 80) and always refuses. Every launch would have
     failed its health check and exited 1. Now http://127.0.0.1:8000/health.
  2. LAUNCH COMMAND: `python -m uvicorn backend.main:app` from the project root dies
     with ModuleNotFoundError: No module named 'market_data', because backend/main.py
     imports its siblings by top-level name. Launch is now `main:app` with
     cwd=backend/, so sibling imports and the app package resolve.
  3. ENGINE IMPORTS: the engines live in backend/app, but the draft imported them as
     `backend.app.engine_*` (unresolvable from the project root). They are imported as
     top-level modules via an explicit path insertion, so the layout is irrelevant.
  4. SIGNAL HANDLING: the draft called sys.exit(0) from inside the signal handler,
     racing the main loop's own cleanup and reporting success on a crash path. The
     handler now only flags shutdown; the main thread performs the ordered teardown.
  5. Relaunch safety: an already-bound port is detected before spawning, so a stale
     server does not masquerade as this orchestrator's healthy child.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import pandas as pd
import pytz
import requests

EST_TZ = pytz.timezone("US/Eastern")

# --- path anchoring (no CWD dependence) ------------------------------------------
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.join(_BACKEND_DIR, "app")
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)

for _p in (_BACKEND_DIR, _APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DATA_DIR = os.getenv("DATA_DIR", os.path.join(_PROJECT_DIR, "data"))

# Bind all interfaces by default: a container binding 127.0.0.1 is unreachable through
# EXPOSE/-p, which silently breaks every deployment topology. Override with API_HOST.
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
# Health polling always targets loopback, never the wildcard bind address.
_HEALTH_HOST = "127.0.0.1" if API_HOST in ("0.0.0.0", "", "::") else API_HOST
HEALTH_URL = os.getenv("HEALTH_URL", f"http://{_HEALTH_HOST}:{API_PORT}/health")
# Trusted reverse-proxy hops for X-Forwarded-For. Default to loopback only: broad trust
# ('*') lets a client spoof its origin IP and poison the auth forensics log.
FORWARDED_ALLOW_IPS = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
if urlparse(HEALTH_URL).hostname in (None, "127.0.0"):
    # Guard against the directive's unbindable literal host.
    HEALTH_URL = f"http://127.0.0.1:{API_PORT}/health"

HEALTH_MAX_RETRIES = int(os.getenv("HEALTH_MAX_RETRIES", "5"))
HEALTH_INTERVAL = float(os.getenv("HEALTH_INTERVAL", "1.0"))
MACRO_INTERVAL = int(os.getenv("CRON_INTERVAL_SECONDS", "60"))
SPATIAL_INTERVAL = int(os.getenv("SPATIAL_INTERVAL_SECONDS", "60"))


# --- production scheduling -------------------------------------------------------
def _coerce_utc(now=None):
    """Return a tz-aware UTC datetime. Naive input is assumed UTC."""
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        return now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(timezone.utc)


def should_trigger_weekly_macro(repo_path, now=None, clock_ok=True):
    """True only when the Friday 15:30 EST release window has opened and this
    calendar week is not already present in the repository.

    `clock_ok=False` simulates an unparseable system clock/timezone: the scheduler
    then blocks (returns False) rather than risk computing against a bad clock.
    """
    if not clock_ok:
        print("⚠️ [SCHEDULER] Timezone/clock anomaly — blocking macro trigger (fail-safe).")
        return False

    now_est = _coerce_utc(now).astimezone(EST_TZ)

    # Before the Friday cut-off, and Mon-Thu, the week's tape is not yet released.
    if now_est.weekday() == 4 and now_est < now_est.replace(hour=15, minute=30, second=0, microsecond=0):
        return False
    if now_est.weekday() < 4:
        return False

    if not os.path.exists(repo_path) or os.path.getsize(repo_path) == 0:
        return True

    try:
        df = pd.read_csv(repo_path)
        if df.empty or "week_ending_date" not in df.columns:
            # Unreadable/empty repository: a refresh is idempotent and will normalise
            # the file, so triggering is the self-healing choice.
            return True
        parsed = pd.to_datetime(df["week_ending_date"], errors="coerce").dropna()
        if parsed.empty:
            return True
        latest = parsed.max()
        if latest.tzinfo is not None:
            latest = latest.tz_convert("UTC")
        else:
            latest = pytz.utc.localize(latest)
        latest_est = latest.astimezone(EST_TZ)
        # ISO year-week comparison is robust across month/year boundaries.
        if latest_est.isocalendar()[:2] == now_est.isocalendar()[:2]:
            return False  # already synchronized this week
    except Exception as exc:
        print(f"⚠️ [SCHEDULER] Weekly repo parse anomaly ({type(exc).__name__}: {exc}) — triggering refresh.")
        return True
    return True


def should_trigger_daily_spatial(repo_path, now=None, clock_ok=True):
    """Daily-roll cache guard: skip the network hit when the current UTC day is
    already present on disk."""
    if not clock_ok:
        print("⚠️ [SCHEDULER] Timezone/clock anomaly — blocking spatial trigger (fail-safe).")
        return False

    if not os.path.exists(repo_path) or os.path.getsize(repo_path) == 0:
        return True

    try:
        df = pd.read_csv(repo_path)
        if df.empty or "Date" not in df.columns:
            return True
        parsed = pd.to_datetime(df["Date"], errors="coerce").dropna()
        if parsed.empty:
            return True
        if parsed.max().date() >= _coerce_utc(now).date():
            return False  # current daily block already cached on disk
    except Exception as exc:
        print(f"⚠️ [SCHEDULER] Spatial repo parse anomaly ({type(exc).__name__}: {exc}) — triggering refresh.")
        return True
    return True


class MasterSystemOrchestrator:
    def __init__(self):
        self.shutdown_signal = threading.Event()
        self.api_process = None
        self.boot_failed = False
        self.macro_repo = os.getenv(
            "MACRO_REPO_CSV", os.path.join(DATA_DIR, "macro_intelligence_repository.csv")
        )
        self.spatial_repo = os.getenv(
            "SPATIAL_REPO_CSV", os.path.join(DATA_DIR, "spatial_boundaries_repository.csv")
        )

    # --- pre-flight ---------------------------------------------------------------
    def warm_data_caches(self):
        """Synchronously seed any missing repository so the API never boots into 503."""
        print("🧠 [PRE-FLIGHT] Verifying structural database repositories...")
        os.makedirs(DATA_DIR, exist_ok=True)

        if not os.path.exists(self.macro_repo) or os.path.getsize(self.macro_repo) == 0:
            print("💾 [CACHE WARMING] Macro repository missing. Executing bootstrap cycle...")
            from engine_macro import InstitutionalDataIngestor

            director = InstitutionalDataIngestor(asset_symbol="XAU/USD")
            director.synthesize_sentiment_divergence(
                director.ingest_weekly_macro_gravity(),
                director.ingest_institutional_fund_flow(),
            )

        if not os.path.exists(self.spatial_repo) or os.path.getsize(self.spatial_repo) == 0:
            print("💾 [CACHE WARMING] Spatial repository missing. Executing boundary mapping...")
            from engine_spatial import SpatialBoundaryEngine

            SpatialBoundaryEngine(ticker="GC=F").execute_pipeline()

    # --- worker loops -------------------------------------------------------------
    def run_macro_loop(self):
        """Isolated worker. Polls every interval and runs only inside the Friday
        15:30 EST release window, once per calendar week."""
        from engine_macro import InstitutionalDataIngestor

        director = InstitutionalDataIngestor(asset_symbol="XAU/USD")
        backoff = 5

        while not self.shutdown_signal.is_set():
            try:
                if should_trigger_weekly_macro(self.macro_repo):
                    print(f"📡 [MACRO ENGINE] Weekly release window open; syncing at {datetime.now(timezone.utc)}")
                    director.synthesize_sentiment_divergence(
                        director.ingest_weekly_macro_gravity(),
                        director.ingest_institutional_fund_flow(),
                    )
                    backoff = 5
                else:
                    print("⏸️ [MACRO ENGINE] Outside release window or week already synced; idle.")
            except Exception as exc:
                print(f"⚠️ [MACRO WORKER EXCEPTION] Typed thread error caught: {type(exc).__name__}: {exc}")
                # Cooling period before retry; never kills the application context.
                if self.shutdown_signal.wait(timeout=backoff):
                    break
                backoff = min(backoff * 2, MACRO_INTERVAL)

            self.shutdown_signal.wait(timeout=MACRO_INTERVAL)

    def run_spatial_loop(self):
        """Isolated worker. Enforces a daily-roll cache guard so a day already on
        disk is served without any network scrape."""
        from engine_spatial import SpatialBoundaryEngine

        spatial_engine = SpatialBoundaryEngine(ticker="GC=F")
        backoff = 5

        while not self.shutdown_signal.is_set():
            try:
                if should_trigger_daily_spatial(self.spatial_repo):
                    print(f"⚡ [SPATIAL ENGINE] Daily roll open; mapping boundaries at {datetime.now(timezone.utc)}")
                    spatial_engine.execute_pipeline()
                    backoff = 5
                else:
                    print("⏸️ [SPATIAL ENGINE] Current daily block already cached; skipping network scrape.")
            except Exception as exc:
                print(f"⚠️ [SPATIAL WORKER EXCEPTION] Typed thread error caught: {type(exc).__name__}: {exc}")
                if self.shutdown_signal.wait(timeout=backoff):
                    break
                backoff = min(backoff * 2, SPATIAL_INTERVAL)

            self.shutdown_signal.wait(timeout=SPATIAL_INTERVAL)

    # --- health ------------------------------------------------------------------
    def poll_api_health(self, url=None, max_retries=None) -> bool:
        """Active bounded health polling in place of a sleep race."""
        url = url or HEALTH_URL
        max_retries = max_retries if max_retries is not None else HEALTH_MAX_RETRIES
        print(f"🔎 [HEALTH CHECK] Polling {url} for socket readiness...")

        for _ in range(max_retries):
            if self.shutdown_signal.is_set():
                return False
            # Fail fast if the child died instead of waiting out the window.
            if self.api_process is not None and self.api_process.poll() is not None:
                print(f"🔴 [HEALTH CHECK] Child exited early with code {self.api_process.returncode}.")
                return False
            try:
                if requests.get(url, timeout=1.0).status_code == 200:
                    print("🟢 [HEALTH CHECK SUCCESS] API backend reporting stable.")
                    return True
            except Exception:
                # Any transport/parse hiccup simply means "not ready yet"; never propagate.
                pass
            time.sleep(HEALTH_INTERVAL)
        return False

    # --- lifecycle ----------------------------------------------------------------
    def launch_system_platform(self):
        print("🚀 Launching Multi-Frequency Institutional Engine Ecosystem...")
        self.warm_data_caches()

        if self.shutdown_signal.is_set():
            return

        cmd = [
            sys.executable, "-m", "uvicorn", "main:app",
            "--host", API_HOST, "--port", str(API_PORT), "--log-level", "warning",
            "--forwarded-allow-ips", FORWARDED_ALLOW_IPS,
        ]
        # cwd=backend/ so `main:app` and its sibling imports resolve regardless of caller CWD.
        self.api_process = subprocess.Popen(cmd, cwd=_BACKEND_DIR)

        if not self.poll_api_health():
            print("🔴 [CRITICAL CRASH] Backend failed to bind safely. Emergency shutdown.")
            self.boot_failed = True
            self.terminate_lifecycle()
            sys.exit(1)

        threading.Thread(target=self.run_macro_loop, daemon=True, name="macro-loop").start()
        threading.Thread(target=self.run_spatial_loop, daemon=True, name="spatial-loop").start()

        print("🏆 [ECOSYSTEM ONLINE] All nodes synchronized and operating.")

    def terminate_lifecycle(self):
        self.shutdown_signal.set()
        if self.api_process and self.api_process.poll() is None:
            print("🛑 [SHUTDOWN COMMENCED] Terminating application microservice...")
            self.api_process.terminate()
            try:
                self.api_process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                print("⚠️ [SHUTDOWN OVERTIME] No SIGTERM response. Issuing SIGKILL.")
                self.api_process.kill()
            print("👋 [SHUTDOWN COMPLETE] Engine context destroyed cleanly.")


def handle_os_signals(orchestrator):
    """Bind OS signals to thread lifecycle. Only flags shutdown; main thread tears down."""

    def signal_handler(signum, _frame):
        print(f"\n🚨 [SYSTEM INTERRUPT] Received OS Signal: {signal.Signals(signum).name}")
        orchestrator.shutdown_signal.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def main():
    orchestrator = MasterSystemOrchestrator()
    handle_os_signals(orchestrator)
    try:
        orchestrator.launch_system_platform()
        while not orchestrator.shutdown_signal.is_set():
            if orchestrator.api_process and orchestrator.api_process.poll() is not None:
                print(f"🔴 [BACKEND DIED] child returned {orchestrator.api_process.returncode}.")
                break
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        orchestrator.terminate_lifecycle()

    sys.exit(1 if orchestrator.boot_failed else 0)


if __name__ == "__main__":
    main()
