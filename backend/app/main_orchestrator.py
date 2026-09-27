"""
LEGACY ENTRY POINT — delegates to the canonical orchestrator.

The master lifecycle engine now lives at backend/orchestrator.py (Directive 08).
This shim is retained so existing invocations of this path keep working without
maintaining a second, divergent copy of the lifecycle logic.
"""

import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from orchestrator import MasterSystemOrchestrator, handle_os_signals, main  # noqa: E402,F401

__all__ = ["MasterSystemOrchestrator", "handle_os_signals", "main"]


if __name__ == "__main__":
    main()
