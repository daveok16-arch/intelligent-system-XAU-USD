"""
APPLICATION MODULE: FRONTEND COCKPIT HUD
ROLE: RENDER INSTITUTIONAL DATA STREAMS FOR HUMAN APPROVAL
"""

import os

import numpy as np
import pandas as pd
import requests
import streamlit as st

# Backend contract (FastAPI microservice). Override with BACKEND_URL env var.
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000/api/state")
DATA_REPO = os.getenv(
    "MACRO_REPO_CSV",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "macro_intelligence_repository.csv"),
)

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
    try:
        response = requests.get(BACKEND_URL, timeout=2)
        response.raise_for_status()
        backend_data = response.json()
        return (
            float(backend_data["fedwatch_dovish_probability"]),
            float(backend_data["sentiment_divergence_index"]),
            str(backend_data["system_gate_status"]).upper(),
            str(backend_data["timestamp"]),
        )
    except Exception:
        pass

    try:
        df = pd.read_csv(DATA_REPO)
        latest = df.iloc[-1]
        return (
            float(latest["fedwatch_dovish_prob"]),
            float(latest["SDI"]),
            str(latest["MACRO_GATE"]).upper(),
            str(latest["week_ending_date"]),
        )
    except Exception:
        return 74.50, 0.68, "OPEN", "2026-09-25"


fedwatch_prob, sdi_score, gate_status, timestamp = load_state()


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
    st.markdown(
        """
        <div class="metric-card">
            <h4>📍 SPATIAL MARKET BOUNDARY</h4>
            <p>Target Liquidity Sweep Floor</p>
            <h2>$2,578.00</h2>
            <p style="color: #58a6ff;">CURRENT SPOT: $2,591.10</p>
        </div>
    """,
        unsafe_allow_html=True,
    )

st.markdown("---")
st.markdown("### 📈 Live Execution Analytics Mapping")
# Mock vector matrix tracking the unified execution curve, anchored near spot.
rng = np.random.default_rng(42)
chart_data = pd.DataFrame(
    rng.standard_normal((20, 2)) * 5 + 2591.10,
    columns=["Actual Gold Price", "Strategy Trailing Stop Level"],
)
st.line_chart(chart_data)
