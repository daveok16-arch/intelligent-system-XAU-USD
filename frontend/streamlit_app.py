"""
APPLICATION MODULE: FRONTEND COCKPIT HUD
ROLE: RENDER INSTITUTIONAL DATA STREAMS FOR HUMAN APPROVAL

Data linkage (Directive 07):
  - Macro state   : GET /api/v1/macro-state        -> card 1 & 2, gate banner
  - Spatial bounds: GET /api/v1/spatial-boundaries -> card 3 (sweep floor, ATR)
  - Price history : GET /api/history               -> chart

Each source degrades independently through a fallback chain:
  live v1 backend -> local repository CSV -> last-known baseline.
No source ever fabricates values; an unavailable panel says so rather than
inventing a number.

Directive 07 corrections applied:
  1. The macro localization previously selected df.iloc[-1] (positional). It now
     selects by max date, matching the backend, so out-of-order ingestion is safe.
  2. The spatial card previously read `liquidity_sweep_floor` from the MACRO repo
     (a stale seeded value) and mislabelled it as the spatial boundary. It now
     consumes the real spatial matrix from /api/v1/spatial-boundaries.
  3. The 15-pip label is corrected: $1.50 on gold is 150 pips at $0.01/pip, per the
     standing interbank convention correction.
"""

import os

import pandas as pd
import requests
import streamlit as st

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.getenv("DATA_DIR", os.path.join(_PROJECT_DIR, "data"))

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000")
MACRO_STATE_URL = f"{API_BASE}/api/v1/macro-state"
SPATIAL_URL = f"{API_BASE}/api/v1/spatial-boundaries"
HISTORY_URL = f"{API_BASE}/api/history"

MACRO_REPO = os.getenv("MACRO_REPO_CSV", os.path.join(_DATA_DIR, "macro_intelligence_repository.csv"))
SPATIAL_REPO = os.getenv("SPATIAL_REPO_CSV", os.path.join(_DATA_DIR, "spatial_boundaries_repository.csv"))

# Bearer token for the protected API surface. Read from the same variable the backend uses.
SYSTEM_AUTH_TOKEN = (os.getenv("SYSTEM_AUTH_TOKEN") or "").strip()

SWEEP_BUFFER_USD = 1.50  # 150 pips at $0.01/pip interbank spot convention

# Last-resort baseline. Used only when neither the backend nor any repository answers.
FALLBACK_MACRO = {
    "timestamp": None,
    "fedwatch_dovish_probability": None,
    "sentiment_divergence_index": None,
    "system_gate_status": None,
}
FALLBACK_SPATIAL = {
    "date": None,
    "three_day_high": None,
    "three_day_low": None,
    "sweep_floor": None,
    "atr_14": None,
    "source": None,
}

st.set_page_config(page_title="Institutional Macro Engine", layout="wide")

st.markdown(
    """
    <style>
        .reportview-container { background: #0e1117; }
        .metric-card {
            background-color: #161b22;
            border: 1px solid #30363d;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
        }
        .gate-open { color: #238636; font-weight: bold; font-size: 24px; }
        .gate-closed { color: #da3633; font-weight: bold; font-size: 24px; }
        .stale { color: #d29922; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("📊 XAU/USD Institutional Situational-Awareness Dashboard")

st.subheader("Macro & Liquidity Intelligence Monitor — Observational, Not Advisory")

st.markdown(
    """
    <div style="background-color:#161b22;border:1px solid #d29922;border-left:4px solid #d29922;
                padding:12px 16px;border-radius:6px;margin-bottom:8px;">
        <strong style="color:#d29922;">⚠️ RESEARCH NOTICE — NOT A TRADING SIGNAL</strong><br>
        <span style="color:#c9d1d9;">
        This dashboard reports macro and positioning conditions for situational awareness.
        Its indicator has been backtested over 2000&ndash;2026 (macro gate and spatial sweep)
        and <strong>shows no statistically significant trading edge</strong>. It must not be
        used as a buy/sell signal or to size positions. See docs/SYSTEMS_AUDIT_LOG.md.
        </span>
    </div>
    """,
    unsafe_allow_html=True,
)
st.markdown("---")


# --- loading helpers -------------------------------------------------------------
AUTH_FAILED = {"flag": False}  # set when the API rejects our credentials


def _auth_headers():
    return {"Authorization": f"Bearer {SYSTEM_AUTH_TOKEN}"} if SYSTEM_AUTH_TOKEN else {}


def _get_json(url, params=None, timeout=4):
    """Return parsed JSON or None. Never raises into the render path.

    A 401 is recorded so the HUD can surface a prominent authentication banner rather
    than silently degrading to local data.
    """
    try:
        resp = requests.get(url, params=params, headers=_auth_headers(), timeout=timeout)
        if resp.status_code == 401:
            AUTH_FAILED["flag"] = True
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _latest_by_date(df, date_column):
    """Select the latest row by max date, never by position."""
    parsed = pd.to_datetime(df[date_column], errors="coerce")
    df = df.assign(_parsed=parsed).dropna(subset=["_parsed"])
    if df.empty:
        raise ValueError("no parseable dates")
    return df.loc[df["_parsed"].idxmax()]


def load_macro_state():
    """Macro state via v1 backend -> macro repository -> baseline."""
    live = _get_json(MACRO_STATE_URL)
    if live:
        try:
            return {
                "timestamp": str(live["timestamp"]),
                "fedwatch_dovish_probability": float(live["fedwatch_dovish_probability"]),
                "sentiment_divergence_index": float(live["sentiment_divergence_index"]),
                "system_gate_status": str(live["system_gate_status"]).upper(),
            }, "LIVE /api/v1/macro-state"
        except (KeyError, TypeError, ValueError):
            pass

    try:
        df = pd.read_csv(MACRO_REPO)
        row = _latest_by_date(df, "week_ending_date")
        return {
            "timestamp": str(row["week_ending_date"]),
            "fedwatch_dovish_probability": float(row["fedwatch_dovish_prob"]),
            "sentiment_divergence_index": float(row["SDI"]),
            "system_gate_status": str(row["MACRO_GATE"]).upper(),
        }, "LOCAL MACRO REPOSITORY"
    except Exception:
        return dict(FALLBACK_MACRO), "MACRO BASELINE UNAVAILABLE"


def load_spatial():
    """Spatial boundaries via v1 backend -> spatial repository -> baseline."""
    live = _get_json(SPATIAL_URL)
    if live:
        try:
            return {
                "date": str(live["date"]),
                "three_day_high": float(live["three_day_high"]),
                "three_day_low": float(live["three_day_low"]),
                "sweep_floor": float(live["sweep_floor"]),
                "atr_14": float(live["atr_14"]),
                "source": str(live["source"]),
            }, "LIVE /api/v1/spatial-boundaries"
        except (KeyError, TypeError, ValueError):
            pass

    try:
        df = pd.read_csv(SPATIAL_REPO)
        row = _latest_by_date(df, "Date")
        return {
            "date": str(row["Date"]),
            "three_day_high": float(row["Three_Day_High"]),
            "three_day_low": float(row["Three_Day_Low"]),
            "sweep_floor": float(row["Sweep_Floor"]),
            "atr_14": float(row["ATR_14"]),
            "source": str(row["Source"]),
        }, "LOCAL SPATIAL REPOSITORY"
    except Exception:
        return dict(FALLBACK_SPATIAL), "SPATIAL BASELINE UNAVAILABLE"


def load_history():
    """History is a hardened endpoint: both range and interval are required."""
    data = _get_json(HISTORY_URL, params={"range": "1mo", "interval": "1d"}, timeout=8)
    return data if data and data.get("points") else None


macro, macro_source = load_macro_state()
spatial, spatial_source = load_spatial()
history = load_history()

# Authentication failure is a hard stop: never present local/baseline values as if the
# handshake succeeded. The banner is rendered first and the panels are suppressed.
if AUTH_FAILED["flag"]:
    for _key in ("fedwatch_dovish_probability", "sentiment_divergence_index", "system_gate_status"):
        macro[_key] = None
    spatial = dict(FALLBACK_SPATIAL)
    history = None
    st.markdown(
        '<div style="background-color:#3d1418;border:1px solid #da3633;padding:18px;'
        'border-radius:8px;color:#ff7b72;font-weight:bold;font-size:20px;text-align:center;">'
        '🔴 CRITICAL ERROR: API AUTHENTICATION HANDSHAKE FAILED</div>',
        unsafe_allow_html=True,
    )
    st.caption("No state is displayed. Set SYSTEM_AUTH_TOKEN identically on the API and cockpit.")


def _fmt(value, spec=",.2f", prefix="$"):
    return f"{prefix}{value:{spec}}" if value is not None else "—"


# --- Row 1: macro regime banner ---------------------------------------------------
# These are OBSERVATIONAL regime labels, not entry instructions. The prior wording
# ("SYSTEM ENTER GATE OPEN — SEARCHING FOR LIQUIDITY SWEEPS") implied a trade
# instruction that the backtests did not support.
st.markdown("### 🛡️ Macro Regime Monitor")
gate_status = macro["system_gate_status"]
if gate_status == "OPEN":
    st.markdown(
        '<div class="gate-open">🟢 CONDITIONS MET — FUND FLOW &amp; MACRO BOTH FAVOURABLE '
        '(observational only)</div>',
        unsafe_allow_html=True,
    )
elif gate_status == "CLOSED":
    st.markdown(
        '<div class="gate-closed">🔴 CONDITIONS NOT MET — MACRO FILTER UNFAVOURABLE '
        '(observational only)</div>',
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        '<div class="gate-closed">⚪ MACRO STATE UNAVAILABLE — NO AUTHORITATIVE RECORD</div>',
        unsafe_allow_html=True,
    )

st.caption(f"Last Institutional Data Package Synchronized On: {macro['timestamp'] or 'unavailable'}")
st.markdown("##")


# --- Row 2: pillar KPI cards -----------------------------------------------------
col1, col2, col3 = st.columns(3)

with col1:
    dovish = macro["fedwatch_dovish_probability"]
    st.markdown(
        f"""
        <div class="metric-card">
            <h4>🌐 GLOBAL MACRO GRAVITY</h4>
            <p>Policy-Path Dovish Pivot Proxy</p>
            <h2>{f"{dovish:.1f}%" if dovish is not None else "—"}</h2>
            <p class="stale">FRED 2y-vs-policy logistic proxy (not CME FedWatch)</p>
        </div>
    """,
        unsafe_allow_html=True,
    )

with col2:
    sdi = macro["sentiment_divergence_index"]
    if sdi is None:
        sdi_color, sdi_label, sdi_text = "#8b949e", "DATA UNAVAILABLE", "—"
    else:
        sdi_color = "#58a6ff"
        # Descriptive positioning bands, not a recommendation.
        sdi_label = ("NET POSITIONING: UPPER RANGE" if sdi > 0.50
                     else "NET POSITIONING: LOWER RANGE")
        sdi_text = f"{sdi:+.2f}"
    st.markdown(
        f"""
        <div class="metric-card">
            <h4>📊 INSTITUTIONAL FUND FLOW</h4>
            <p>Commercial Positioning Percentile (SDI)</p>
            <h2 style="color: {sdi_color};">{sdi_text}</h2>
            <p style="color: {sdi_color};">{sdi_label}</p>
            <p style="color: #8b949e;">Percentile of the trailing 52-week range</p>
        </div>
    """,
        unsafe_allow_html=True,
    )

with col3:
    floor = spatial["sweep_floor"]
    low = spatial["three_day_low"]
    atr = spatial["atr_14"]
    st.markdown(
        f"""
        <div class="metric-card">
            <h4>📍 SPATIAL MARKET BOUNDARY</h4>
            <p>3-Day Structural Low → Reference Level</p>
            <h2>{_fmt(floor)}</h2>
            <p style="color: #58a6ff;">3D LOW: {_fmt(low)} · OFFSET: ${SWEEP_BUFFER_USD:.2f} (150 pips)</p>
            <p style="color: #8b949e;">ATR(14): {f"{atr:,.2f}" if atr is not None else "—"}</p>
            <p style="color: #8b949e;">Reference geometry only — not an entry level</p>
        </div>
    """,
        unsafe_allow_html=True,
    )

st.caption(
    f"Macro source: {macro_source} · Spatial source: {spatial_source} · "
    f"Spatial as of {spatial['date'] or 'unavailable'}"
)
st.markdown("---")


# --- Row 3: price history --------------------------------------------------------
st.markdown("### 📈 Gold Price History")
if history:
    hist_df = pd.DataFrame(history["points"])
    hist_df["date"] = pd.to_datetime(hist_df["date"])
    hist_df = hist_df.set_index("date").rename(columns={"close": f"{history['symbol']} close (USD)"})
    st.line_chart(hist_df)
    st.caption(
        f"{history['instrument']} ({history['symbol']}) · {history['interval']} · {history['range']} · "
        f"source: {history['source']} — futures proxy, not XAU/USD spot. Fetched {history['as_of']}."
    )
else:
    st.warning("Live price history feed unavailable — chart suppressed rather than showing synthetic data.")
