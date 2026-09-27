"""
APPLICATION MODULE: MARKET DATA ADAPTER
ROLE: FETCH LIVE GOLD PRICE FEEDS FOR THE COCKPIT, WITH TTL CACHING AND GRACEFUL DEGRADATION

Sources:
  - Spot XAU/USD : https://api.gold-api.com/price/XAU  (no key required)
  - Daily history: https://query2.finance.yahoo.com/v8/finance/chart/GC=F  (COMEX gold futures)

XAU/USD spot history is not offered by any keyless public API, so the daily
series is COMEX gold futures (GC=F), a standard proxy. Callers must label it
as such -- it trades at a basis to spot and is not the same instrument.
"""

import os
import time
from datetime import datetime, timezone

import requests

SPOT_URL = os.getenv("SPOT_URL", "https://api.gold-api.com/price/XAU")
HISTORY_URL = os.getenv(
    "HISTORY_URL", "https://query2.finance.yahoo.com/v8/finance/chart/GC=F"
)
HISTORY_SYMBOL = os.getenv("HISTORY_SYMBOL", "GC=F")
HTTP_TIMEOUT = float(os.getenv("MARKET_HTTP_TIMEOUT", "8"))
_UA = {"User-Agent": "Mozilla/5.0 (compatible; InstitutionalMacroEngine/0.1)"}

SPOT_TTL = float(os.getenv("SPOT_TTL", "30"))
HISTORY_TTL = float(os.getenv("HISTORY_TTL", "900"))

# Tiny process-local TTL cache: keeps the cockpit from hammering upstream feeds.
_cache: dict[str, tuple[float, object]] = {}


def _cached(key, ttl, producer):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        value = producer()
    except Exception:
        # Serve stale data rather than nothing, if we have any.
        return hit[1] if hit else None
    _cache[key] = (now, value)
    return value


def _fetch_spot():
    resp = requests.get(SPOT_URL, timeout=HTTP_TIMEOUT, headers=_UA)
    resp.raise_for_status()
    payload = resp.json()
    price = float(payload["price"])
    return {
        "price": round(price, 2),
        "symbol": "XAU/USD",
        "source": "gold-api.com",
        "as_of": payload.get("updatedAt") or datetime.now(timezone.utc).isoformat(),
    }


def _fetch_history(range_: str, interval: str):
    resp = requests.get(
        HISTORY_URL,
        params={"range": range_, "interval": interval},
        timeout=HTTP_TIMEOUT,
        headers=_UA,
    )
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    stamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]

    points = []
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        points.append(
            {
                "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                "close": round(float(close), 2),
            }
        )
    if not points:
        raise ValueError("no usable history points returned")
    return {
        "symbol": result["meta"].get("symbol", HISTORY_SYMBOL),
        "instrument": result["meta"].get("shortName", "Gold Futures"),
        "kind": "FUTURES_PROXY",
        "source": "query2.finance.yahoo.com",
        "interval": interval,
        "range": range_,
        "points": points,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def get_spot():
    """Live XAU/USD spot, or None if the feed is unreachable."""
    return _cached("spot", SPOT_TTL, _fetch_spot)


def get_history(range_="1mo", interval="1d"):
    """Daily gold-futures closes, or None if the feed is unreachable."""
    return _cached(f"history:{range_}:{interval}", HISTORY_TTL, lambda: _fetch_history(range_, interval))
