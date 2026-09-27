"""
APPLICATION MODULE: MASTER PIPELINE ORCHESTRATOR
ROLE: SYSTEM SKELETAL SYNC, ASYNC CRON-TASKS, AND API LIFECYCLE MANAGEMENT
AUTHORITY: AUTOMATED QUANTITATIVE PLATFORM DIRECTOR

Lifecycle:
  1. Pre-flight: ensure the macro repository exists and is ingestible.
  2. Launch the FastAPI microservice as a child process and wait until /health answers.
  3. Start the background macro-ingestion daemon on an isolated thread.
  4. Hold the platform open; terminate children cleanly on Ctrl+C.

Deviations from the original directive draft (all deliberate):
  - Resolves the repository by absolute path; the original checked a CWD-relative
    filename that never matched, so pre-flight silently never ran.
  - Launches uvicorn via `sys.executable -m uvicorn` with cwd=backend/. The bare
    `uvicorn` binary is not on PATH in this environment and `main:app` only resolves
    from the backend directory.
  - Replaces `time.sleep(2)` with a real /health poll; a fixed sleep races the socket.
  - Removes the `os.rename(data_ingestion_engine.py -> engine_macro.py)` hack; the
    engine module exists as engine_macro.py and renaming files at startup is unsafe.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.request import urlopen

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_APP_DIR)
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)

if _APP_DIR not in sys.path:
    sys.path.append(_APP_DIR)

REPO_PATH = os.getenv(
    "MACRO_REPO_CSV",
    os.path.join(_PROJECT_DIR, "data", "macro_intelligence_repository.csv"),
)
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))
HEALTH_URL = f"http://{API_HOST}:{API_PORT}/health"

# Sandbox cadence. In production this shifts to a weekly trigger matching the
# Friday CFTC 15:30 EST tape release.
CRON_INTERVAL_SECONDS = int(os.getenv("CRON_INTERVAL_SECONDS", "60"))
HEALTH_TIMEOUT_SECONDS = float(os.getenv("HEALTH_TIMEOUT_SECONDS", "30"))


class MasterSystemOrchestrator:
    def __init__(self):
        self.system_active = True
        self.api_process = None
        self._stop_event = threading.Event()
        print("⚙️ [MASTER ORCHESTRATOR INITIALIZED] -> Setting up Institutional Data Gateway...")

    def _install_signal_handlers(self):
        """Translate SIGTERM/SIGINT into a clean stop. SIGTERM is what systemd,
        Docker, and Kubernetes actually send -- the original only caught Ctrl+C."""

        def _handle(signum, _frame):
            print(f"\n📶 [SIGNAL {signum}] -> Capture received, initiating ordered shutdown.")
            self.system_active = False
            self._stop_event.set()

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)

    # --- background worker -------------------------------------------------------
    def run_async_macro_cron(self):
        """
        Background Worker Thread: runs the institutional data dump on a loop,
        recalculating the Sentiment Divergence Index and refreshing the repository.
        """
        print("🧠 [CRON WORKER STARTED] -> Background Macro Ingestion Daemon Active.")
        while self.system_active:
            try:
                print(f"📡 [BACKGROUND SYNC] -> Triggering Core Data Ingestion Tapes at {datetime.now(timezone.utc)}")

                # Imported locally so a failed import doesn't poison the API process.
                from engine_macro import InstitutionalDataIngestor

                director = InstitutionalDataIngestor(asset_symbol="XAU/USD")
                macro_df = director.ingest_weekly_macro_gravity()
                cot_df = director.ingest_institutional_fund_flow()
                row = director.synthesize_sentiment_divergence(macro_df, cot_df)

                print(
                    f"✅ [BACKGROUND SYNC SUCCESS] -> Repository refreshed for {row['week_ending_date']} "
                    f"(SDI {row['SDI']}, gate {row['MACRO_GATE']}). Next loop in {CRON_INTERVAL_SECONDS}s."
                )
            except Exception as e:
                print(f"⚠️ [CRON ERROR] -> Failed to execute background ingestion loop: {e}")

            # Interruptible sleep so shutdown isn't delayed by a full interval.
            self._stop_event.wait(CRON_INTERVAL_SECONDS)

    # --- API lifecycle -----------------------------------------------------------
    def launch_backend_api_server(self):
        """Spawn the FastAPI microservice as a child process and await readiness."""
        print(f"🚀 [LAUNCHING BACKEND SERVER] -> Exposing FastAPI secure microservice on port {API_PORT}...")
        try:
            cmd = [
                sys.executable,
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                API_HOST,
                "--port",
                str(API_PORT),
                "--log-level",
                "warning",
            ]
            # cwd=backend/ so `main:app` resolves and relative imports line up.
            self.api_process = subprocess.Popen(cmd, cwd=_BACKEND_DIR)
        except Exception as e:
            print(f"🔴 [BACKEND API CRITICAL ERROR] -> Failed to mount API process server: {e}")
            self.system_active = False
            return False

        if self._await_api_health():
            print(f"🟢 [BACKEND API ONLINE] -> Endpoints bound to http://{API_HOST}:{API_PORT}")
            return True

        print("🔴 [BACKEND API CRITICAL ERROR] -> Health endpoint never became ready.")
        self.system_active = False
        return False

    def _await_api_health(self):
        """Poll /health until it answers or the timeout elapses."""
        deadline = time.time() + HEALTH_TIMEOUT_SECONDS
        while time.time() < deadline and self.system_active:
            if self.api_process.poll() is not None:
                print(f"🔴 [BACKEND API EXITED] -> child process returned {self.api_process.returncode}")
                return False
            try:
                with urlopen(HEALTH_URL, timeout=2) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                time.sleep(0.5)
        return False

    # --- pre-flight --------------------------------------------------------------
    def _preflight(self):
        """Ensure the repository exists and carries at least one usable row."""
        if os.path.exists(REPO_PATH) and os.path.getsize(REPO_PATH) > 0:
            return True

        print("⚠️ [PRE-FLIGHT WARNING] -> Data repository missing. Triggering initial ingestion cycle...")
        try:
            from engine_macro import InstitutionalDataIngestor

            director = InstitutionalDataIngestor(asset_symbol="XAU/USD")
            director.synthesize_sentiment_divergence(
                director.ingest_weekly_macro_gravity(),
                director.ingest_institutional_fund_flow(),
            )
            return True
        except Exception as e:
            print(f"🔴 [PRE-FLIGHT FAILED] -> Could not seed the repository: {e}")
            return False

    def _shutdown(self):
        print("\n🚨 [SYSTEM SHUTDOWN COMMENCED] -> Terminating platform hooks safely...")
        self.system_active = False
        if self.api_process and self.api_process.poll() is None:
            self.api_process.terminate()
            try:
                self.api_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.api_process.kill()
            print("🛑 [PROCESS CLEARED] -> Backend FastAPI server disconnected.")
        print("👋 [SHUTDOWN COMPLETE] -> Master Orchestrator offline.")

    # --- main loop ---------------------------------------------------------------
    def orchestrate_platform_lifecycle(self):
        self._install_signal_handlers()

        if not self._preflight():
            self.system_active = False
            return

        if not self.launch_backend_api_server():
            self._shutdown()
            return

        cron_thread = threading.Thread(target=self.run_async_macro_cron, daemon=True, name="macro-cron")
        cron_thread.start()

        print("\n🏆 [PLATFORM FULLY OPERATIONAL] -> Master Orchestrator holding network threads open. Press Ctrl+C to terminate.")
        try:
            while self.system_active:
                if self.api_process.poll() is not None:
                    print(f"🔴 [BACKEND API DIED] -> child returned {self.api_process.returncode}; shutting down.")
                    break
                self._stop_event.wait(1)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self._shutdown()


if __name__ == "__main__":
    MasterSystemOrchestrator().orchestrate_platform_lifecycle()
