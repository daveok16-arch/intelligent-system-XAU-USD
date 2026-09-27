"""
APPLICATION MODULE: SPATIAL BOUNDARY MAPPING ENGINE
ROLE: COMPUTE STRUCTURAL 3-DAY SWING COORDINATES AND THE 15-PIP SWEEP BUFFER ZONE

Produces, per session, the rolling structural high/low and the liquidity sweep
floor (Three_Day_Low - 1.50) derived from authentic GC=F history via yfinance.

Corrections applied to the Directive 04 draft (each verified against live data):
  1. Path: the draft targeted /workspaces/gold_intelligence_app, which does not
     exist in this container. Real project root is /workspace/project.
  2. ATR_14 is NOT fabricated. The draft used .fillna(10.0), which stamped a
     constant 10.0 onto 10 of 18 output rows and presented it as a true-range
     mean. ATR is now a real 14-period mean; rows without full lookback are
     excluded rather than filled.
  3. The fallback no longer manufactures prices. The draft synthesized a constant
     OHLC series and wrote it to disk as if it were market data. The engine now
     serves a previously cached REAL payload and labels its Source as CACHE_*;
     with no cache it raises, refusing to invent data.
  4. Date is emitted as ISO-8601 STRING per the declared contract (draft emitted
     datetime64).
  5. OHLC nulls raise a typed ValueError instead of contaminating state.
  6. History window widened to 3mo so the 14-period ATR has enough sessions to be
     genuinely non-null in the output.
"""

import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

_SWEEP_BUFFER_USD = 1.50  # 15 pips at XAU $0.10/pip scaling
_ATR_WINDOW = 14
_SWING_WINDOW = 3
_MIN_SESSIONS = _ATR_WINDOW + 2  # 1 shift + 14 window, plus one to satisfy the window edge

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)
_DATA_DIR = os.getenv("DATA_DIR", os.path.join(_PROJECT_DIR, "data"))

# orchestrator.py (which owns the schedule guards) lives in backend/. Make it
# importable regardless of whether this module is run as a script or `-m` package.
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


class SpatialDataUnavailable(RuntimeError):
    """No authentic market history is reachable and no real cache exists."""


class SpatialBoundaryEngine:
    def __init__(self, ticker="GC=F", data_dir=None):
        self.ticker = ticker
        self.data_dir = data_dir or _DATA_DIR
        self.cache_path = os.path.join(self.data_dir, "spatial_raw_cache.csv")
        self.output_path = os.path.join(self.data_dir, "spatial_boundaries_repository.csv")

    # --- source acquisition ------------------------------------------------------
    def fetch_market_history(self, retries=3, backoff=2.0):
        """Fetch authentic OHLC history, with bounded retries then a REAL-data cache.

        Returns (dataframe, source_label). Never returns fabricated prices.
        """
        last_error = None
        for attempt in range(retries):
            try:
                df = yf.download(
                    self.ticker, period="3mo", interval="1d", progress=False, auto_adjust=False
                )
                df = self._normalize(df)
                self._validate_ohlc(df)
                self._write_cache(df)
                return df, f"YFINANCE_{self.ticker}"
            except Exception as exc:
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))

        cached = self._load_cache()
        if cached is not None:
            print(f"⚠️ [ENDPOINT ERRORED] -> {last_error}. Serving cached real payload.")
            return cached, f"CACHE_{self.ticker}"

        raise SpatialDataUnavailable(
            f"live {self.ticker} history unavailable after {retries} attempts and no real "
            f"cache exists at {self.cache_path}: {last_error}"
        )

    @staticmethod
    def _normalize(df):
        """Flatten yfinance MultiIndex columns and give the index a stable name."""
        if df is None:
            raise ValueError("no payload returned from market node")
        if isinstance(df.columns, pd.MultiIndex):
            df = df.copy()
            df.columns = df.columns.get_level_values(0)
        df = df.reset_index()
        if "Date" not in df.columns and "index" in df.columns:
            df = df.rename(columns={"index": "Date"})
        return df

    @staticmethod
    def _validate_ohlc(df):
        required = ["Date", "High", "Low", "Close"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"payload missing required columns: {missing}")
        for column in ("High", "Low", "Close"):
            if df[column].isna().any():
                raise ValueError(f"required OHLC column '{column}' contains null entries")
        if df.empty:
            raise ValueError("payload is empty")

    # --- cache (real data only) --------------------------------------------------
    def _write_cache(self, df):
        os.makedirs(self.data_dir, exist_ok=True)
        df.to_csv(self.cache_path, index=False)

    def _load_cache(self):
        if not os.path.exists(self.cache_path) or os.path.getsize(self.cache_path) == 0:
            return None
        try:
            cached = pd.read_csv(self.cache_path)
            self._validate_ohlc(cached)
            return cached
        except Exception:
            return None

    # --- computation -------------------------------------------------------------
    def calculate_boundaries(self, df, source=None):
        """Compute structural swing coordinates. Pure function of the input frame.

        `source` labels provenance. It defaults to the live label for this ticker,
        but callers serving cached data must pass `CACHE_<ticker>` so a fallback
        payload is never misrepresented as a live fetch.
        """
        source = source or f"YFINANCE_{self.ticker}"
        if len(df) < _MIN_SESSIONS:
            raise ValueError(
                f"insufficient history: need >= {_MIN_SESSIONS} sessions for a 3-day swing "
                f"and 14-period ATR, received {len(df)}"
            )

        df = df.sort_values("Date").reset_index(drop=True)
        for column in ("High", "Low", "Close"):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        # Nulls were already rejected upstream; guard against coercion introducing new ones.
        if df[["High", "Low", "Close"]].isna().any().any():
            raise ValueError("OHLC values could not be coerced to numeric without nulls")

        # Shift by one session so each row uses only information available before it.
        df["Three_Day_High"] = df["High"].shift(1).rolling(_SWING_WINDOW).max()
        df["Three_Day_Low"] = df["Low"].shift(1).rolling(_SWING_WINDOW).min()

        high_low = df["High"] - df["Low"]
        high_prev_close = (df["High"] - df["Close"].shift(1)).abs()
        low_prev_close = (df["Low"] - df["Close"].shift(1)).abs()
        true_range = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)

        # Real 14-period mean. No fillna: an unfilled ATR is honest, a filled one is not.
        df["ATR_14"] = true_range.shift(1).rolling(_ATR_WINDOW).mean()

        df["Sweep_Floor"] = df["Three_Day_Low"] - _SWEEP_BUFFER_USD
        df["Source"] = source

        output = df[
            ["Date", "Three_Day_High", "Three_Day_Low", "Sweep_Floor", "ATR_14", "Source"]
        ].dropna()
        output = output.copy()
        output["Date"] = pd.to_datetime(output["Date"]).dt.strftime("%Y-%m-%d")
        return output.sort_values("Date", ascending=False).reset_index(drop=True)

    # --- pipeline ----------------------------------------------------------------
    def execute_pipeline(self):
        raw_df, source = self.fetch_market_history()
        processed_df = self.calculate_boundaries(raw_df, source)
        os.makedirs(self.data_dir, exist_ok=True)
        processed_df.to_csv(self.output_path, index=False)
        return processed_df


def main(argv=None):
    """Single-shot spatial mapping entry point for the Kubernetes CronJob.

    Honours the daily-roll cache guard so a job that fires repeatedly within one UTC
    day performs no redundant upstream scrape. Exits non-zero on failure.

    Usage: python -m engine_spatial [--force]
    """
    import argparse

    parser = argparse.ArgumentParser(description="Daily spatial boundary mapping (single shot).")
    parser.add_argument("--force", action="store_true", help="Bypass the daily-roll cache guard.")
    args = parser.parse_args(argv)

    engine = SpatialBoundaryEngine(ticker="GC=F")

    if not args.force:
        try:
            from orchestrator import should_trigger_daily_spatial

            if not should_trigger_daily_spatial(engine.output_path):
                print("⏸️ [SPATIAL JOB] Current daily block already cached. No-op.")
                return 0
        except Exception as exc:
            print(f"⚠️ [SPATIAL JOB] Schedule guard unavailable ({type(exc).__name__}: {exc}); aborting.")
            return 1

    try:
        out = engine.execute_pipeline()
        print(f"✅ [SPATIAL JOB] Boundaries mapped: {len(out)} rows, latest {out.iloc[0]['Date']}.")
        return 0
    except SpatialDataUnavailable as exc:
        print(f"🔴 [SPATIAL JOB] No authentic history available: {exc}")
        return 1
    except Exception as exc:
        print(f"🔴 [SPATIAL JOB] Mapping failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
