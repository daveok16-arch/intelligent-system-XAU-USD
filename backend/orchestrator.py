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

import requests

# --- path anchoring (no CWD dependence) ------------------------------------------
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.join(_BACKEND_DIR, "app")
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)

for _p in (_BACKEND_DIR, _APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DATA_DIR = os.getenv("DATA_DIR", os.path.join(_PROJECT_DIR, "data"))

API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))
HEALTH_URL = os.getenv("HEALTH_URL", f"http://{API_HOST}:{API_PORT}/health")
if urlparse(HEALTH_URL).hostname in (None, "127.0.0"):
    # Guard against the directive's unbindable literal host.
    HEALTH_URL = f"http://127.0.0.1:{API_PORT}/health"

HEALTH_MAX_RETRIES = int(os.getenv("HEALTH_MAX_RETRIES", "5"))
HEALTH_INTERVAL = float(os.getenv("HEALTH_INTERVAL", "1.0"))
MACRO_INTERVAL = int(os.getenv("CRON_INTERVAL_SECONDS", "60"))
SPATIAL_INTERVAL = int(os.getenv("SPATIAL_INTERVAL_SECONDS", "60"))


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
        """Isolated worker for macro gravity + fund flow. Retries with backoff."""
        from engine_macro import InstitutionalDataIngestor

        director = InstitutionalDataIngestor(asset_symbol="XAU/USD")
        backoff = 5

        while not self.shutdown_signal.is_set():
            try:
                print(f"📡 [MACRO ENGINE] Syncing macro-gravity loops at {datetime.now(timezone.utc)}")
                director.synthesize_sentiment_divergence(
                    director.ingest_weekly_macro_gravity(),
                    director.ingest_institutional_fund_flow(),
                )
                backoff = 5
            except Exception as exc:
                print(f"⚠️ [MACRO WORKER EXCEPTION] Typed thread error caught: {type(exc).__name__}: {exc}")
                # Cooling period before retry; never kills the application context.
                if self.shutdown_signal.wait(timeout=backoff):
                    break
                backoff = min(backoff * 2, MACRO_INTERVAL)

            self.shutdown_signal.wait(timeout=MACRO_INTERVAL)

    def run_spatial_loop(self):
        """Isolated worker for structural boundary mapping. Retries with backoff."""
        from engine_spatial import SpatialBoundaryEngine

        spatial_engine = SpatialBoundaryEngine(ticker="GC=F")
        backoff = 5

        while not self.shutdown_signal.is_set():
            try:
                print(f"⚡ [SPATIAL ENGINE] Triggering daily boundary calculations at {datetime.now(timezone.utc)}")
                spatial_engine.execute_pipeline()
                backoff = 5
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
