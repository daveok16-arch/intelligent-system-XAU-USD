"""
APPLICATION MODULE: BACKEND CORE MICROSERVICE
ROLE: SERVE MACRO INTELLIGENCE STATE AND SPATIAL BOUNDARY MATRICES TO THE COCKPIT

Routes:
  Legacy (consumed by frontend/streamlit_app.py):
    GET /api/state, GET /api/spot, GET /api/history, POST /api/refresh, GET /health
  v1 (Directive 06 contract):
    GET /api/v1/macro-state        -> MacroStateResponse
    GET /api/v1/spatial-boundaries -> SpatialBoundariesResponse

Directive 06 corrections, each verified against the live container:
  1. The draft targeted backend/app/main.py, which does not exist; the running app is
     backend/main.py. It also anchored repos at backend/app/, but the repositories live
     in data/. With the draft's BASE_DIR every route would have returned 503.
  2. `allow_origins=["*"]` with `allow_credentials=True` is spec-invalid: Starlette then
     echoes the request origin instead of `*`, so the draft's own CORS assertion fails.
     Credentials are dropped (this API is read-only and cookie-free), origins are
     env-configurable, and methods are narrowed to GET (least privilege).
  3. Repository readers return 503 for missing/empty/unusable data and purge NaN rows
     before they can reach the JSON layer.
"""

import hmac
import logging
import os
import secrets

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("institutional.api")

import market_data

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.getenv("DATA_DIR", os.path.join(_PROJECT_DIR, "data"))

MACRO_REPO = os.getenv("MACRO_REPO_CSV", os.path.join(_DATA_DIR, "macro_intelligence_repository.csv"))
SPATIAL_REPO = os.getenv(
    "SPATIAL_REPO_CSV", os.path.join(_DATA_DIR, "spatial_boundaries_repository.csv")
)

# Comma-separated origins. Defaults to wildcard for the read-only dashboard;
# override with CORS_ALLOW_ORIGINS=https://cockpit.example.com in production.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]

app = FastAPI(
    title="Institutional Situational-Awareness API (XAU/USD)",
    description=(
        "Observational macro and positioning data for XAU/USD. "
        "RESEARCH NOTICE: the indicator served here was backtested over 2000-2026 "
        "(macro gate and spatial sweep) and shows no statistically significant trading "
        "edge. It is situational-awareness data, not a buy/sell signal. "
        "See docs/SYSTEMS_AUDIT_LOG.md."
    ),
    version="2.0.0",
)

# Read-only public data surface: GET only, no credentials. Wildcard origins are only
# valid when allow_credentials is False.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# --- authentication ---------------------------------------------------------------
# Stateless bearer auth for the entire /api/* surface.
#
# Directive 10 deviations (both are security-critical):
#   1. The draft protected only /api/v1/* and /api/history -- leaving /api/state, which
#      returns the IDENTICAL macro payload, wide open. Protection is enforced by
#      middleware across all /api/* paths, so a new route cannot be added unprotected
#      by omission. /health stays open for orchestrator socket polling.
#   2. The draft fell back to a hardcoded token when SYSTEM_AUTH_TOKEN was unset. That
#      bakes a public secret into the image. This fails CLOSED instead.

_BEARER_PREFIX = "bearer "
_OPEN_PATHS = {"/health", "/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect"}


def system_auth_token():
    """The configured bearer token, or None when none is provisioned."""
    token = (os.getenv("SYSTEM_AUTH_TOKEN") or "").strip()
    return token or None


def _generate_ephemeral_token():
    """Dev-only fallback: a random per-process token, logged once."""
    token = secrets.token_urlsafe(32)
    logger.warning(
        "[AUTH] SYSTEM_AUTH_TOKEN is not set. Generated an ephemeral per-process token "
        "for this run only. Set SYSTEM_AUTH_TOKEN for a stable credential."
    )
    return token


def _unauthorized(request, detail):
    """Emit a spec-compliant challenge. Never leaks whether the token was near-correct."""
    logger.warning("[AUTH] Rejected %s %s from %s", request.method, request.url.path, request.client.host if request.client else "unknown")
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.middleware("http")
async def enforce_bearer_auth(request: Request, call_next):
    path = request.url.path
    if path in _OPEN_PATHS or request.method == "OPTIONS" or not path.startswith("/api/"):
        return await call_next(request)

    configured = system_auth_token()
    expected = configured or _ephemeral_token()

    header = request.headers.get("Authorization", "")
    if not header.lower().startswith(_BEARER_PREFIX):
        return _unauthorized(request, "Missing or malformed Authorization header.")

    presented = header[len(_BEARER_PREFIX):].strip()
    # Constant-time comparison resists timing oracles.
    if not hmac.compare_digest(presented, expected):
        return _unauthorized(request, "Invalid signature matrix. Institutional access denied.")

    return await call_next(request)


_ephemeral_token_cache = {}


def _ephemeral_token():
    if "value" not in _ephemeral_token_cache:
        _ephemeral_token_cache["value"] = _generate_ephemeral_token()
    return _ephemeral_token_cache["value"]


class MacroStateResponse(BaseModel):
    timestamp: str
    fedwatch_dovish_probability: float
    sentiment_divergence_index: float
    system_gate_status: str


class SpatialBoundariesResponse(BaseModel):
    date: str
    three_day_high: float
    three_day_low: float
    sweep_floor: float
    atr_14: float
    source: str


def _read_repository(file_path, required_columns, date_candidates):
    """Load a repository and return its latest row by max date.

    503 for missing/empty/unusable data; 500 only for genuinely unexpected faults.
    """
    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Repository node initializing. Pipeline data not yet synchronized.",
        )
    try:
        df = pd.read_csv(file_path)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Repository unreadable while pipeline initializes: {exc}",
        )

    if df.empty:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Target repository layer contains zero entries.",
        )

    date_col = next((c for c in date_candidates if c in df.columns), None)
    if date_col is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Repository missing a date column (looked for {date_candidates}).",
        )

    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Repository missing required columns: {missing}",
        )

    # Purge malformed rows before they can become corrupted JSON tokens downstream.
    df = df.dropna(subset=required_columns + [date_col])
    if df.empty:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No complete records remain after purging malformed rows.",
        )

    parsed = pd.to_datetime(df[date_col], errors="coerce")
    df = df.assign(_parsed_date=parsed).dropna(subset=["_parsed_date"])
    if df.empty:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Repository contains no parseable dates.",
        )

    # Max-date selection: never positional, so out-of-order ingestion is safe.
    return df.loc[df["_parsed_date"].idxmax()]


# ------------------------------------------------------------------ legacy surface
@app.get("/health")
def health():
    return {"status": "ok"}


def _latest_macro_state():
    record = _read_repository(
        MACRO_REPO,
        required_columns=["fedwatch_dovish_prob", "SDI", "MACRO_GATE", "spot_price", "liquidity_sweep_floor"],
        date_candidates=["week_ending_date", "Date"],
    )
    spot = float(record["spot_price"])
    floor = float(record["liquidity_sweep_floor"])
    # A zero floor is not a valid boundary; reporting 0.0% distance would look like a
    # real reading. Report null so the cockpit shows "—" instead of a plausible lie.
    distance_pct = round(((spot - floor) / floor * 100.0), 3) if floor else None
    live = market_data.get_spot()
    return {
        "fedwatch_dovish_probability": float(record["fedwatch_dovish_prob"]),
        "sentiment_divergence_index": float(record["SDI"]),
        "system_gate_status": str(record["MACRO_GATE"]).upper(),
        "timestamp": record["_parsed_date"].strftime("%Y-%m-%d"),
        "spot_price": round(spot, 2),
        "liquidity_sweep_floor": round(floor, 2),
        "distance_to_floor_pct": distance_pct,
        "data_source": str(record.get("source", "UNKNOWN")),
        "market_spot": live["price"] if live else None,
        "market_spot_source": live["source"] if live else None,
        "market_spot_as_of": live["as_of"] if live else None,
    }


@app.get("/api/state")
def get_state():
    """Legacy state route retained for the existing cockpit frontend.

    The `force_gate` test override has been removed: it allowed any caller to flip
    the gate banner, which is a presentation-integrity hazard on a decision surface.
    """
    return JSONResponse(_latest_macro_state())


@app.post("/api/refresh")
def refresh():
    """No cached repository state remains; retained for backward compatibility."""
    return {"status": "cache_cleared"}


@app.get("/api/spot")
def get_spot():
    spot = market_data.get_spot()
    if not spot:
        raise HTTPException(status_code=503, detail="live spot feed unavailable")
    return spot


@app.get("/api/history")
def get_history(
    range_: str = Query(
        ...,
        alias="range",
        pattern=r"^\d+(d|mo|y)$",
        description="Required. Window such as 5d, 1mo, 3mo, 1y.",
    ),
    interval: str = Query(
        ...,
        pattern=r"^\d+(m|h|d|wk|mo)$",
        description="Required. Bar interval such as 1d, 1h, 15m, 1wk.",
    ),
):
    """Price history. Both parameters are required and range-bounded so callers
    cannot request arbitrary upstream windows."""
    # Bound the range so a caller cannot request an unbounded upstream scrape.
    allowed_ranges = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y"}
    allowed_intervals = {"1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"}
    if range_ not in allowed_ranges:
        raise HTTPException(status_code=422, detail=f"range must be one of {sorted(allowed_ranges)}")
    if interval not in allowed_intervals:
        raise HTTPException(status_code=422, detail=f"interval must be one of {sorted(allowed_intervals)}")

    history = market_data.get_history(range_, interval)
    if not history:
        raise HTTPException(status_code=503, detail="history feed unavailable")
    return history


# ---------------------------------------------------------------------- v1 surface
@app.get("/api/v1/macro-state", response_model=MacroStateResponse)
def get_macro_state():
    state = _latest_macro_state()
    return {
        "timestamp": state["timestamp"],
        "fedwatch_dovish_probability": state["fedwatch_dovish_probability"],
        "sentiment_divergence_index": state["sentiment_divergence_index"],
        "system_gate_status": state["system_gate_status"],
    }


@app.get("/api/v1/spatial-boundaries", response_model=SpatialBoundariesResponse)
def get_spatial_boundaries():
    record = _read_repository(
        SPATIAL_REPO,
        required_columns=["Three_Day_High", "Three_Day_Low", "Sweep_Floor", "ATR_14", "Source"],
        date_candidates=["Date", "week_ending_date"],
    )
    return {
        "date": record["_parsed_date"].strftime("%Y-%m-%d"),
        "three_day_high": float(record["Three_Day_High"]),
        "three_day_low": float(record["Three_Day_Low"]),
        "sweep_floor": float(record["Sweep_Floor"]),
        "atr_14": float(record["ATR_14"]),
        "source": str(record["Source"]),
    }
