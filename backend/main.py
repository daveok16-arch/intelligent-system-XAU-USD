"""
APPLICATION MODULE: BACKEND CORE MICROSERVICE
ROLE: SERVE MACRO INTELLIGENCE STATE TO THE FRONTEND COCKPIT HUD

Contract (consumed by frontend/streamlit_app.py):
    GET /api/state ->
        {
          "fedwatch_dovish_probability": float,   # percent, 0-100
          "sentiment_divergence_index": float,    # -1 .. +1 (unbounded in practice)
          "system_gate_status": "OPEN" | "CLOSED",
          "timestamp": str,
          "spot_price": float,
          "liquidity_sweep_floor": float,
          "distance_to_floor_pct": float          # signed % from spot to floor
        }
"""

import os
from functools import lru_cache

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import market_data

DATA_REPO = os.getenv(
    "MACRO_REPO_CSV",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "macro_intelligence_repository.csv"),
)

app = FastAPI(title="Institutional Macro Engine", version="0.1.0")

# The cockpit HUD is served from a different origin (Streamlit), so allow it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@lru_cache(maxsize=1)
def _read_repo():
    """Load and validate the macro repository. Cached; clear on refresh."""
    if not os.path.exists(DATA_REPO):
        raise FileNotFoundError(DATA_REPO)
    df = pd.read_csv(DATA_REPO)
    required = {
        "week_ending_date",
        "fedwatch_dovish_prob",
        "SDI",
        "MACRO_GATE",
        "spot_price",
        "liquidity_sweep_floor",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"repository missing columns: {sorted(missing)}")
    if df.empty:
        raise ValueError("repository is empty")
    return df


def _latest_state():
    try:
        df = _read_repo()
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="macro repository unavailable")
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    latest = df.iloc[-1]
    spot = float(latest["spot_price"])
    floor = float(latest["liquidity_sweep_floor"])
    distance_pct = ((spot - floor) / floor * 100.0) if floor else 0.0

    # Live spot is advisory: the repository remains the authoritative state.
    live = market_data.get_spot()

    return {
        "fedwatch_dovish_probability": float(latest["fedwatch_dovish_prob"]),
        "sentiment_divergence_index": float(latest["SDI"]),
        "system_gate_status": str(latest["MACRO_GATE"]).upper(),
        "timestamp": str(latest["week_ending_date"]),
        "spot_price": round(spot, 2),
        "liquidity_sweep_floor": round(floor, 2),
        "distance_to_floor_pct": round(distance_pct, 3),
        "market_spot": live["price"] if live else None,
        "market_spot_source": live["source"] if live else None,
        "market_spot_as_of": live["as_of"] if live else None,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/state")
def get_state(
    force_gate: str | None = Query(
        default=None,
        description="Testing override for gate status: 'OPEN' or 'CLOSED'.",
    ),
):
    state = _latest_state()
    if force_gate:
        gate = force_gate.upper()
        if gate not in {"OPEN", "CLOSED"}:
            raise HTTPException(status_code=400, detail="force_gate must be OPEN or CLOSED")
        state["system_gate_status"] = gate
    return JSONResponse(state)


@app.post("/api/refresh")
def refresh():
    """Drop the cached repository so the next read picks up new data."""
    _read_repo.cache_clear()
    return {"status": "cache_cleared"}


@app.get("/api/spot")
def get_spot():
    """Live XAU/USD spot price. 503 if the upstream feed is unreachable."""
    spot = market_data.get_spot()
    if not spot:
        raise HTTPException(status_code=503, detail="live spot feed unavailable")
    return spot


@app.get("/api/history")
def get_history(
    range_: str = Query(default="1mo", alias="range", pattern=r"^\d+(d|mo|y)$"),
    interval: str = Query(default="1d", pattern=r"^\d+(m|h|d|wk|mo)$"),
):
    """Daily gold-futures closes (proxy series). 503 if upstream is unavailable."""
    history = market_data.get_history(range_, interval)
    if not history:
        raise HTTPException(status_code=503, detail="history feed unavailable")
    return history
