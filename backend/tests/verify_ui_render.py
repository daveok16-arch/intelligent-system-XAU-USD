"""
SYSTEM AUDIT & VISUAL RENDER VERIFICATION (Directive 11)

Projects the live Streamlit cockpit as an ASCII map from the ACTUAL repository files,
so the terminal shows what the HUD is really rendering -- not a mock-up.

Corrected from the directive draft, which could not run:
  1. It used `null`, which is not Python: `if macro is not null` raises NameError.
     Python uses `None`.
  2. `df[date_col] = pd.to_datetime(df[date_col])` raises on an unparseable date rather
     than degrading gracefully; parsing now uses errors="coerce".
  3. The legend block referenced columns the real HUD does not show (Swing High/Low were
     in the directive's example but the live cockpit renders the 3-Day Structural Low,
     the $1.50 buffer and ATR(14)). Labels and fields below mirror
     frontend/streamlit_app.py exactly so the projection is faithful.
  4. The directive hardcoded paths; they are resolved through DATA_DIR as every other
     module does.

Read-only. Fabricates nothing: a missing or unreadable repository renders a clean
unpopulated state rather than plausible numbers.
"""

import os
import sys

import pandas as pd

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

_DATA_DIR = os.getenv("DATA_DIR", os.path.join(_PROJECT_DIR, "data"))
MACRO_PATH = os.getenv("MACRO_REPO_CSV", os.path.join(_DATA_DIR, "macro_intelligence_repository.csv"))
SPATIAL_PATH = os.getenv("SPATIAL_REPO_CSV", os.path.join(_DATA_DIR, "spatial_boundaries_repository.csv"))

SWEEP_BUFFER_USD = 1.50  # 150 pips at $0.01/pip interbank convention
DASH = "—"

# Must match the cockpit's thresholds exactly, or the projection would disagree with
# the screen it claims to represent.
SDI_ACCUMULATING_THRESHOLD = 0.50
DOVISH_LABEL_THRESHOLD = 0.50


def parse_latest_record(path, date_col):
    """Latest row by max date, or None. Never raises; never fabricates."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    try:
        df = pd.read_csv(path)
        if df.empty or date_col not in df.columns:
            return None
        df = df.assign(_d=pd.to_datetime(df[date_col], errors="coerce")).dropna(subset=["_d"])
        if df.empty:
            return None
        return df.loc[df["_d"].idxmax()]
    except Exception:
        return None


def _money(value):
    return f"${float(value):,.2f}" if value is not None else DASH


def _num(value):
    return f"{float(value):,.2f}" if value is not None else DASH


def collect_state():
    """Read both repositories. Returns a dict of display-ready strings."""
    macro = parse_latest_record(MACRO_PATH, "week_ending_date")
    spatial = parse_latest_record(SPATIAL_PATH, "Date")

    state = {
        "macro_present": macro is not None,
        "spatial_present": spatial is not None,
        "gate": None, "fedwatch": DASH, "dovish_label": DASH, "sdi": DASH,
        "sdi_label": DASH, "sdi_color_ok": None, "macro_date": DASH, "source": DASH,
        "low": DASH, "floor": DASH, "atr": DASH, "spatial_date": DASH, "spatial_source": DASH,
    }

    if macro is not None:
        state["gate"] = str(macro["MACRO_GATE"]).upper()
        dovish_pct = float(macro["fedwatch_dovish_prob"])
        sdi_value = float(macro["SDI"])
        state["fedwatch"] = f"{dovish_pct:.1f}%"
        state["dovish_label"] = "DOVISH" if (dovish_pct / 100.0) > DOVISH_LABEL_THRESHOLD else "HAWKISH"
        state["sdi"] = f"{sdi_value:+.2f}"  # matches the HUD's {sdi:+.2f}
        state["sdi_label"] = ("SMART MONEY ACCUMULATING"
                              if sdi_value > SDI_ACCUMULATING_THRESHOLD
                              else "SMART MONEY DISTRIBUTING")
        state["sdi_color_ok"] = sdi_value > SDI_ACCUMULATING_THRESHOLD
        state["macro_date"] = str(macro["week_ending_date"])
        state["source"] = str(macro.get("source", "UNKNOWN"))

    if spatial is not None:
        state["low"] = _money(spatial["Three_Day_Low"])
        state["floor"] = _money(spatial["Sweep_Floor"])
        state["atr"] = _num(spatial["ATR_14"])
        state["spatial_date"] = str(spatial["Date"])
        state["spatial_source"] = str(spatial.get("Source", "UNKNOWN"))

    return state


def render(state):
    """ASCII projection mirroring the live HUD layout."""
    if state["gate"] == "OPEN":
        banner = "🟢 SYSTEM ENTER GATE OPEN — SEARCHING FOR SESSION LIQUIDITY SWEEPS"
    elif state["gate"] == "CLOSED":
        banner = "🔴 SYSTEM ENTER GATE CLOSED — MACRO BIAS INACTIVE"
    else:
        banner = "⚪ SYSTEM STATE UNAVAILABLE — NO AUTHORITATIVE MACRO RECORD"

    W = 88
    line = "─" * (W - 2)

    def box(text):
        return f"│ {text:<{W - 4}} │"

    out = []
    C1, C2, C3 = 28, 30, 26  # column content widths

    def three(a, b, c):
        return box(f"{a:<{C1}}│ {b:<{C2}}│ {c:<{C3}}")

    out.append("┌" + line + "┐")
    out.append(box("📊 XAU/USD UNIFIED STRATEGY COCKPIT — LIVE RENDER PROJECTION"))
    out.append("├" + line + "┤")
    out.append(box(""))
    out.append(box("🛡️  SYSTEM EXECUTIVE CONTROL STATE"))
    out.append(box(f"   {banner}"))
    out.append(box(""))
    out.append(box(f"   Macro record as of {state['macro_date']}   (source: {state['source']})"))
    out.append("├" + line + "┤")
    out.append(three("🌐 GLOBAL MACRO GRAVITY", "📊 INSTITUTIONAL FUND FLOW", "📍 SPATIAL MARKET BOUNDARY"))
    out.append(three("Policy-Path Dovish Pivot Proxy", "Sentiment Divergence (SDI)", "3-Day Structural Low → Floor"))
    out.append(three(f"Value: {state['fedwatch']}", f"Value: {state['sdi']}", f"3D LOW: {state['low']}"))
    out.append(three(f"Bias:  {state['dovish_label']}", state["sdi_label"], f"FLOOR: {state['floor']}"))
    out.append(three("(FRED proxy, not", "", f"ATR(14): {state['atr']}"))
    out.append(three(" CME FedWatch)", "", f"BUFFER: ${SWEEP_BUFFER_USD:.2f} (150 pips)"))
    out.append("├" + line + "┤")
    out.append(box("📈 GOLD PRICE HISTORY"))
    out.append(box(f"   Spatial record as of {state['spatial_date']}   (source: {state['spatial_source']})"))
    out.append(box(""))
    if not state["macro_present"] or not state["spatial_present"]:
        missing = []
        if not state["macro_present"]:
            missing.append("macro repository")
        if not state["spatial_present"]:
            missing.append("spatial repository")
        out.append(box(f"⚠️  DEGRADED: {' and '.join(missing)} unavailable — panels show {DASH}"))
    out.append("└" + line + "┘")
    return "\n".join(out)


def generate_live_cockpit_view():
    state = collect_state()
    print(render(state))
    return state


if __name__ == "__main__":
    generate_live_cockpit_view()
