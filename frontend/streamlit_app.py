"""
APPLICATION MODULE: FRONTEND COCKPIT HUD
ROLE: RENDER INSTITUTIONAL DATA STREAMS FOR HUMAN APPROVAL
"""

import os

import pandas as pd
import requests
import streamlit as st

# Backend contract (FastAPI microservice). Override with BACKEND_URL env var.
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000/api/state")
HISTORY_API_URL = os.getenv("HISTORY_API_URL", "http://127.0.0.1:8000/api/history")
# Fallback repository lives in the shared data/ dir at the repo root.
DATA_REPO = os.getenv(
    "MACRO_REPO_CSV",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "macro_intelligence_repository.csv"),
)

# Baseline used only when neither the backend nor the repository is reachable.
FALLBACK_STATE = {
    "fedwatch_dovish_probability": 74.50,
    "sentiment_divergence_index": 0.68,
    "system_gate_status": "OPEN",
    "timestamp": "2026-09-25",
    "spot_price": 2591.10,
    "liquidity_sweep_floor": 2578.00,
    "market_spot": None,
    "market_spot_source": None,
    "market_spot_as_of": None,
}

# Set professional terminal configurations
st.set_page_config(page_title="Institutional Macro Engine", layout="wide")

# Apply dark-themed CSS styling to mirror a Bloomberg or Reuters terminal layout
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
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("📊 XAU/USD Unified Strategy Cockpit")
st.subheader("Institutional Portfolio Management & Macro Intelligence Terminal")
st.markdown("---")


# --- DATA LINKAGE TO BACKEND MICROSERVICE ---
# In production this queries our FastAPI endpoint. A resilient fallback chain
# keeps the cockpit rendering even when the backend is cycling.
def load_state():
    """Return cockpit state, preferring the live backend then the repository."""
    source = "BASELINE DEFAULT"
    try:
        response = requests.get(BACKEND_URL, timeout=2)
        response.raise_for_status()
        d = response.json()
        return {
            "fedwatch_dovish_probability": float(d["fedwatch_dovish_probability"]),
            "sentiment_divergence_index": float(d["sentiment_divergence_index"]),
            "system_gate_status": str(d["system_gate_status"]).upper(),
            "timestamp": str(d["timestamp"]),
            "spot_price": float(d["spot_price"]),
            "liquidity_sweep_floor": float(d["liquidity_sweep_floor"]),
            "market_spot": d.get("market_spot"),
            "market_spot_source": d.get("market_spot_source"),
            "market_spot_as_of": d.get("market_spot_as_of"),
        }, "LIVE BACKEND"
    except Exception:
        pass

    try:
        df = pd.read_csv(DATA_REPO)
        latest = df.iloc[-1]
        return {
            "fedwatch_dovish_probability": float(latest["fedwatch_dovish_prob"]),
            "sentiment_divergence_index": float(latest["SDI"]),
            "system_gate_status": str(latest["MACRO_GATE"]).upper(),
            "timestamp": str(latest["week_ending_date"]),
            "spot_price": float(latest["spot_price"]),
            "liquidity_sweep_floor": float(latest["liquidity_sweep_floor"]),
            "market_spot": None,
            "market_spot_source": None,
            "market_spot_as_of": None,
        }, "DATA REPOSITORY"
    except Exception:
        return dict(FALLBACK_STATE), source


state, data_source = load_state()
fedwatch_prob = state["fedwatch_dovish_probability"]
sdi_score = state["sentiment_divergence_index"]
gate_status = state["system_gate_status"]
timestamp = state["timestamp"]
spot_price = state["spot_price"]
sweep_floor = state["liquidity_sweep_floor"]
market_spot = state.get("market_spot")
market_spot_as_of = state.get("market_spot_as_of")


def load_history():
    """Fetch real daily gold-futures history from the backend. None if unavailable."""
    try:
        resp = requests.get(HISTORY_API_URL, params={"range": "1mo", "interval": "1d"}, timeout=8)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


# --- RENDER COCKPIT LAYOUT BLOCKS ---

# Row 1: High Level System Status Banner
st.markdown("### 🛡️ System Executive Control State")
if gate_status == "OPEN":
    st.markdown(
        '<div class="gate-open">🟢 SYSTEM ENTER GATE OPEN — SEARCHING FOR SESSION LIQUIDITY SWEEPS</div>',
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        '<div class="gate-closed">🔴 SYSTEM ENTER GATE CLOSED — MACRO BIAS INACTIVE</div>',
        unsafe_allow_html=True,
    )

st.caption(f"Last Institutional Data Package Synchronized On: {timestamp}")
st.markdown("##")


# Row 2: Pillar Metric KPI Cards
col1, col2, col3 = st.columns(3)

with col1:
    st.markdown(
        f"""
        <div class="metric-card">
            <h4>🌐 GLOBAL MACRO GRAVITY</h4>
            <p>CME FedWatch Dovish Pivot Prob</p>
            <h2>{fedwatch_prob:.1f}%</h2>
            <p style="color: #238636;">STATUS: REGIME ACCELERATING</p>
        </div>
    """,
        unsafe_allow_html=True,
    )

with col2:
    # Visual color shift keyed to the mathematical threshold (> 0.50 = accumulation).
    sdi_color = "#238636" if sdi_score > 0.50 else "#da3633"
    sdi_label = "SMART MONEY ACCUMULATING" if sdi_score > 0.50 else "SMART MONEY DISTRIBUTING"
    st.markdown(
        f"""
        <div class="metric-card">
            <h4>📊 INSTITUTIONAL FUND FLOW</h4>
            <p>Sentiment Divergence Index (SDI)</p>
            <h2 style="color: {sdi_color};">{sdi_score:+.2f}</h2>
            <p style="color: {sdi_color};">{sdi_label}</p>
        </div>
    """,
        unsafe_allow_html=True,
    )

with col3:
    dist = ((spot_price - sweep_floor) / sweep_floor * 100.0) if sweep_floor else 0.0
    dist_color = "#238636" if dist > 0 else "#da3633"
    if market_spot is not None:
        live_line = f'<p style="color: #58a6ff;">LIVE SPOT: ${market_spot:,.2f}</p>'
    else:
        live_line = '<p style="color: #8b949e;">LIVE SPOT: feed unavailable</p>'
    st.markdown(
        f"""
        <div class="metric-card">
            <h4>📍 SPATIAL MARKET BOUNDARY</h4>
            <p>Target Liquidity Sweep Floor</p>
            <h2>${sweep_floor:,.2f}</h2>
            {live_line}
            <p style="color: {dist_color};">STRATEGY SPOT: ${spot_price:,.2f} ({dist:+.2f}%)</p>
        </div>
    """,
        unsafe_allow_html=True,
    )

st.markdown("---")
st.markdown("### 📈 Gold Price History")
history = load_history()

if history and history.get("points"):
    hist_df = pd.DataFrame(history["points"])
    hist_df["date"] = pd.to_datetime(hist_df["date"])
    hist_df = hist_df.set_index("date")
    hist_df = hist_df.rename(columns={"close": f"{history['symbol']} close (USD)"})
    st.line_chart(hist_df)
    st.caption(
        f"{history['instrument']} ({history['symbol']}) · {history['interval']} · "
        f"{history['range']} · source: {history['source']} — futures proxy, not XAU/USD spot. "
        f"Fetched {history['as_of']}."
    )
else:
    st.warning("Live price history feed unavailable — chart suppressed rather than showing synthetic data.")
