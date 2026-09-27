"""
APPLICATION MODULE: HISTORICAL REFERENCE STORE
ROLE: PERSIST THE MULTI-DECADE MACRO / POSITIONING / PRICE HISTORY THE DASHBOARD NEEDS

The current repositories hold only the latest state (4 macro rows, 49 spatial rows).
That is enough to render a snapshot, but a situational-awareness dashboard needs history:
trend, percentile context, and change over time.

This module builds three persisted, append-safe history files under data/history/:

  macro_history.csv    weekly CFTC commercial positioning + FRED-derived dovish proxy,
                       each week's own macro regime, and a rolling 52-week SDI percentile
  price_history.csv    daily GC=F OHLC (the underlying the dashboard charts)
  spatial_history.csv  daily structural boundaries (3-day swing, offset reference, ATR)

Design rules (consistent with the rest of the platform):
  - Only real, fetched data. Missing inputs raise; nothing is synthesised.
  - Look-ahead free: every derived value uses only prior observations.
  - Idempotent: re-running replaces the rows it recomputes rather than duplicating.

Usage:
    python -m backend.app.history_store            # incremental refresh
    python -m backend.app.history_store --rebuild  # full rebuild from source
"""

import os
import sys

import numpy as np
import pandas as pd

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_DIR = os.path.join(_BACKEND_DIR, "app")
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)
for _p in (_BACKEND_DIR, _APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine_macro import InstitutionalDataIngestor, SDI_REFERENCE_WEEKS  # noqa: E402
import backtest_spatial  # noqa: E402

DATA_DIR = os.getenv("DATA_DIR", os.path.join(_PROJECT_DIR, "data"))
HISTORY_DIR = os.getenv("HISTORY_DIR", os.path.join(DATA_DIR, "history"))

MACRO_HISTORY = os.path.join(HISTORY_DIR, "macro_history.csv")
PRICE_HISTORY = os.path.join(HISTORY_DIR, "price_history.csv")
SPATIAL_HISTORY = os.path.join(HISTORY_DIR, "spatial_history.csv")

PRICE_START = os.getenv("HISTORY_PRICE_START", "2000-08-30")


class HistoryUnavailable(RuntimeError):
    """A required source could not be read; nothing is written."""


def _atomic_write(df, path):
    """Write via a temp file so a crash cannot leave a half-written history."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def _load_existing(path):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            return pd.read_csv(path)
        except Exception:
            return None
    return None


# --- macro / positioning history -------------------------------------------------
def build_macro_history(rebuild=False):
    """Weekly positioning + macro regime + rolling SDI percentile, look-ahead free."""
    ingestor = InstitutionalDataIngestor(asset_symbol="XAU/USD")
    cot = ingestor.ingest_institutional_fund_flow(weeks=SDI_REFERENCE_WEEKS * 40)  # full depth
    macro = ingestor.ingest_weekly_macro_gravity()
    if cot is None or cot.empty:
        raise HistoryUnavailable("COT positioning history unavailable")

    cot = cot.sort_values("week_ending_date").reset_index(drop=True)
    cot["week_date"] = pd.to_datetime(cot["week_ending_date"], errors="coerce")
    cot = cot.dropna(subset=["week_date"])

    # Attach the macro regime prevailing at each COT week (backward as-of: a week sees
    # only macro information available up to that date).
    macro = macro.assign(macro_date=pd.to_datetime(macro["log_date"], errors="coerce")).dropna(
        subset=["macro_date"]).sort_values("macro_date")
    merged = pd.merge_asof(
        cot.sort_values("week_date"),
        macro[["macro_date", "fedwatch_dovish_prob", "policy_target_rate", "two_year_yield",
               "central_bank_bias"]],
        left_on="week_date", right_on="macro_date", direction="backward",
    )

    # Rolling percentile of commercial positioning: each week ranked only against the
    # trailing window. Mirrors the production SDI definition.
    roll = merged["Commercial_Net"].rolling(SDI_REFERENCE_WEEKS, min_periods=SDI_REFERENCE_WEEKS)
    merged["SDI"] = ((merged["Commercial_Net"] - roll.min()) / (roll.max() - roll.min())).clip(0, 1).round(4)
    # These are bounds of the COMMERCIAL NET positioning over the window, not of the SDI
    # (the SDI is itself a 0-1 percentile of them). Named explicitly to avoid the earlier
    # confusion that produced a nonsense 266% "percentile of range".
    merged["Commercial_Net_52w_Low"] = roll.min()
    merged["Commercial_Net_52w_High"] = roll.max()

    merged["dovish"] = merged["fedwatch_dovish_prob"] / 100.0
    merged["MACRO_GATE"] = np.where((merged["SDI"] > 0.50) & (merged["dovish"] > 0.50), "OPEN", "CLOSED")
    merged["source"] = "CFTC_COT+FRED"

    out = merged[[
        "week_ending_date", "week_date", "Large_Spec_Net", "Commercial_Net",
        "open_interest", "fedwatch_dovish_prob", "central_bank_bias",
        "SDI", "Commercial_Net_52w_Low", "Commercial_Net_52w_High", "MACRO_GATE", "source",
    ]].rename(columns={"week_date": "date"})
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    out = out.dropna(subset=["Commercial_Net"]).reset_index(drop=True)

    if not rebuild:
        existing = _load_existing(MACRO_HISTORY)
        if existing is not None and len(existing) > len(out):
            print(f"⚠️  existing macro history is longer ({len(existing)}) than the fetch "
                  f"({len(out)}); keeping the existing file rather than truncating it.")
            return existing
    _atomic_write(out, MACRO_HISTORY)
    return out


# --- price history ---------------------------------------------------------------
def build_price_history(rebuild=False):
    """Daily GC=F OHLC. Fetched, never synthesised."""
    bars = backtest_spatial.SpatialEdgeBacktester().load_daily_bars()
    if bars is None or bars.empty:
        raise HistoryUnavailable("daily OHLC unavailable")
    out = bars.copy()
    out["date"] = pd.to_datetime(out["Date"]).dt.strftime("%Y-%m-%d")
    out = out[["date", "Open", "High", "Low", "Close"]].rename(
        columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"})
    out["source"] = f"YFINANCE_{backtest_spatial.SpatialEdgeBacktester().ticker}"
    out = out.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    _atomic_write(out, PRICE_HISTORY)
    return out


# --- spatial boundary history ----------------------------------------------------
def build_spatial_history(rebuild=False):
    """Daily structural boundaries over the FULL price history, look-ahead free.

    Derived from the persisted price history rather than the engine's 3-month fetch, so
    the boundary series spans the same multi-decade period as the price chart instead of
    the last quarter.
    """
    prices = _load_existing(PRICE_HISTORY)
    if prices is None or prices.empty:
        prices = build_price_history(rebuild=True)
    df = prices.copy()
    df["Date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)
    for col, lower in (("high", "High"), ("low", "Low"), ("close", "Close")):
        if lower not in df.columns:
            df[lower] = df[col]
    if len(df) < 20:
        raise HistoryUnavailable(f"price history too short for boundaries: {len(df)} rows")

    df["Three_Day_High"] = df["High"].shift(1).rolling(3).max()
    df["Three_Day_Low"] = df["Low"].shift(1).rolling(3).min()
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift(1)).abs()
    lc = (df["Low"] - df["Close"].shift(1)).abs()
    df["ATR_14"] = pd.concat([hl, hc, lc], axis=1).max(axis=1).shift(1).rolling(14).mean()
    df["Sweep_Floor"] = df["Three_Day_Low"] - _SWEEP_BUFFER

    out = df[["Date", "Three_Day_High", "Three_Day_Low", "Sweep_Floor", "ATR_14"]].dropna().copy()
    out["date"] = out["Date"].dt.strftime("%Y-%m-%d")
    out["source"] = f"DERIVED_FROM_{pd.read_csv(PRICE_HISTORY)['source'].iloc[-1] if os.path.exists(PRICE_HISTORY) else 'YFINANCE_GC=F'}"
    out = out[["date", "Three_Day_High", "Three_Day_Low", "Sweep_Floor", "ATR_14", "source"]]
    _atomic_write(out, SPATIAL_HISTORY)
    return out


_SWEEP_BUFFER = float(os.getenv("SWEEP_BUFFER_USD", "1.50"))


def summary():
    lines = []
    for label, path in (("macro", MACRO_HISTORY), ("price", PRICE_HISTORY), ("spatial", SPATIAL_HISTORY)):
        df = _load_existing(path)
        if df is None or df.empty:
            lines.append(f"  {label:<8} (empty)")
        else:
            lines.append(f"  {label:<8} {len(df):>6} rows   {df['date'].iloc[0]} → {df['date'].iloc[-1]}")
    return "\n".join(lines)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Build/refresh the historical reference store.")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild from source, ignoring existing files.")
    args = parser.parse_args(argv)

    os.makedirs(HISTORY_DIR, exist_ok=True)
    print("📚 [HISTORY STORE] building reference history...")
    results = {}
    for name, fn in (("macro", build_macro_history), ("price", build_price_history),
                     ("spatial", build_spatial_history)):
        try:
            df = fn(rebuild=args.rebuild)
            results[name] = len(df)
            print(f"  ✅ {name}: {len(df)} rows")
        except Exception as exc:
            print(f"  🔴 {name}: FAILED — {type(exc).__name__}: {exc}")
            return 1
    print("\n📦 [HISTORY STORE] contents:")
    print(summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
