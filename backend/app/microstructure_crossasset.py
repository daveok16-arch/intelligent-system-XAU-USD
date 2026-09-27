"""
SYSTEM MODULE: CROSS-ASSET REPLICATION OF THE SWEEP VOLATILITY EFFECT
ROLE: TEST WHETHER THE FINDING IS A MARKET PROPERTY OR A GOLD-SPECIFIC ARTEFACT

Why this is the decisive test
-----------------------------
The walk-forward study found that sweeps of rolling swing lows are followed by a larger
absolute move (median 1.43x on high-volume sweeps, 3/5 folds significant, zero negative
folds). One strong negative result and one strong positive result were both obtained on
the SAME instrument with the SAME parameters. That is exactly the situation where a
single-asset finding deserves suspicion: with enough slicing, gold alone may have offered
a structure that does not generalise.

Cross-asset replication is the standard remedy. If the same effect appears, with the same
sign and comparable magnitude, in instruments with different participant bases, it is a
property of how these markets trade. If it appears only in gold (or only in the two
precious metals, which share participants), it is more likely a coincidence of one series.

Assets used, chosen to differ in structure:
  GC=F  gold futures        (precious metal, deep speculative + commercial flow)
  SI=F  silver futures      (precious metal, thinner, more retail-driven)
  PL=F  platinum futures    (precious metal, largely industrial demand)
  HG=F  copper futures      (industrial metal, different participant base entirely)

Copper is the strongest control: it shares almost no demand/positioning story with gold,
so a shared effect there cannot be a "gold narrative" artefact.

Method: identical to the walk-forward study -- same sweep definition, same volume
threshold, same horizons, prior-bar levels only, unconditional null. No parameter is
retuned per asset, because tuning per asset would defeat the purpose.
"""

import os
import sys

import numpy as np
import pandas as pd
import yfinance as yf

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

DATA_DIR = os.getenv("DATA_DIR", os.path.join(_PROJECT_DIR, "data"))
HISTORY_DIR = os.getenv("HISTORY_DIR", os.path.join(DATA_DIR, "history"))

ASSETS = {
    "GC=F": "gold",
    "SI=F": "silver",
    "PL=F": "platinum",
    "HG=F": "copper",
}
INTERVAL = "1h"
PERIOD = "730d"
VOLUME_ELEVATED_RATIO = float(os.getenv("MS_VOLUME_RATIO", "1.5"))


class ReplicationDataUnavailable(RuntimeError):
    """A required asset series could not be obtained; replication must not be simulated."""


class CrossAssetStudy:
    def __init__(self, interval=INTERVAL, period=PERIOD):
        self.interval = interval
        self.period = period

    def load(self, symbol, refresh=False):
        cache = os.path.join(HISTORY_DIR, f"intraday_{symbol.replace('=', '_')}_{self.interval}.csv")
        if not refresh and os.path.exists(cache) and os.path.getsize(cache) > 0:
            df = pd.read_csv(cache)
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
            df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
            if len(df) > 200:
                return df

        raw = yf.download(symbol, interval=self.interval, period=self.period,
                          progress=False, auto_adjust=False)
        if raw is None or raw.empty:
            raise ReplicationDataUnavailable(f"no intraday bars for {symbol}")
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        for c in ("High", "Low", "Close", "Volume"):
            if c not in raw.columns:
                raise ReplicationDataUnavailable(f"{symbol} payload missing {c}")
        df = raw.reset_index()
        ts_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
        df = df.rename(columns={ts_col: "timestamp", "High": "high", "Low": "low",
                                "Close": "close", "Open": "open", "Volume": "volume"})
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        df = df.dropna(subset=["timestamp", "high", "low", "close"]).sort_values("timestamp")
        df = df[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)

        os.makedirs(HISTORY_DIR, exist_ok=True)
        df.to_csv(cache, index=False)
        return df

    # --- identical method to the walk-forward study --------------------------------
    @staticmethod
    def build(df, lookback=12):
        out = df.copy()
        out["level"] = out["low"].shift(1).rolling(lookback).min()
        lvl = out["level"]
        depth = (lvl - out["low"]) / lvl * 10000.0
        out["is_sweep"] = (out["low"] < lvl) & (depth >= 1.0) & (out["close"] >= lvl)
        out["vol_med"] = out["volume"].shift(1).rolling(50, min_periods=20).median()
        out["vol_ratio"] = out["volume"] / out["vol_med"].replace(0, np.nan)
        out["vol_elevated"] = out["vol_ratio"] >= VOLUME_ELEVATED_RATIO
        return out.dropna(subset=["level", "vol_ratio"]).reset_index(drop=True)

    @staticmethod
    def abs_move(df, horizon=1):
        close = df["close"].to_numpy()
        out = np.full(len(close), np.nan)
        out[:-horizon] = np.abs(close[horizon:] - close[:-horizon]) / close[:-horizon]
        return out

    @staticmethod
    def welch(a, b):
        a = np.asarray(a, dtype=float)[~np.isnan(np.asarray(a, dtype=float))]
        b = np.asarray(b, dtype=float)[~np.isnan(np.asarray(b, dtype=float))]
        if len(a) < 2 or len(b) < 2:
            return float("nan")
        se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
        return float((a.mean() - b.mean()) / se) if se > 0 else float("nan")

    def one_asset(self, symbol):
        df = self.build(self.load(symbol))
        am = self.abs_move(df, 1)
        base = am[~np.isnan(am)]
        out = {"symbol": symbol, "asset": ASSETS.get(symbol, symbol), "bars": len(df),
               "sweeps": int(df["is_sweep"].sum())}
        for name, mask in (("all", df["is_sweep"]),
                           ("highvol", df["is_sweep"] & df["vol_elevated"])):
            vals = am[mask.to_numpy()]
            vals = vals[~np.isnan(vals)]
            out[f"{name}_n"] = int(len(vals))
            if len(vals) >= 20:
                out[f"{name}_ratio"] = round(float(vals.mean() / base.mean()), 3)
                out[f"{name}_t"] = round(self.welch(vals, base), 3)
            else:
                out[f"{name}_ratio"] = None
                out[f"{name}_t"] = None
        return out

    def run(self):
        print(f"🌍 [CROSS-ASSET REPLICATION] interval={self.interval}, period={self.period}")
        print("    Identical parameters across assets -- no per-asset tuning.")
        print("    Expectation if real: same sign, comparable magnitude, across all.\n")
        rows = []
        for sym in ASSETS:
            try:
                rows.append(self.one_asset(sym))
            except ReplicationDataUnavailable as exc:
                print(f"    ⚠️  {sym}: {exc}")
        df = pd.DataFrame(rows)
        cols = ["symbol", "asset", "bars", "sweeps", "all_n", "all_ratio", "all_t",
                "highvol_n", "highvol_ratio", "highvol_t"]
        print(df[[c for c in cols if c in df.columns]].to_string(index=False))
        print()

        hv = df["highvol_ratio"].dropna()
        print("--- replication verdict (high-volume sweeps) ---")
        if len(hv):
            positive = int((hv > 1.0).sum())
            print(f"    assets with ratio > 1.0: {positive}/{len(hv)}  (median {float(hv.median()):.3f}x)")
            sig = int((df['highvol_t'].fillna(0) >= 2.0).sum())
            neg = int((df['highvol_t'].fillna(0) <= -2.0).sum())
            print(f"    significant positive: {sig}   significant negative: {neg}")
            if positive == len(hv) and sig >= len(hv) // 2:
                print("    ✅ REPLICATED: the effect appears in every asset tested, with the")
                print("       same sign. This is a property of these markets, not of one series.")
            elif positive >= len(hv) - 1 and neg == 0:
                print("    ◻️  PARTIAL: directionally consistent but not uniformly strong.")
            else:
                print("    ❌ NOT REPLICATED: the effect does not generalise across assets.")
        return df


def main():
    study = CrossAssetStudy()
    try:
        study.run()
    except ReplicationDataUnavailable as exc:
        print(f"🔴 [REPLICATION ABORTED] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
