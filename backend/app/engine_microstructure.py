"""
SYSTEM MODULE: INTRADAY MICROSTRUCTURE STUDY
ROLE: TEST THE STOP-HUNT / LIQUIDITY-SWEEP THESIS AT THE RESOLUTION IT OPERATES

Why this module exists
----------------------
Directives 13-15 tested the sweep thesis on DAILY bars and found nothing. That result is
real but it does not test the mechanism. The claim is:

    Stops cluster just below obvious swing lows. Large players, needing liquidity to fill
    size, push price into that cluster, trigger the resting sells, buy against them, and
    price then recovers.

On daily bars a "sweep" is only "the low went below a level" -- the push-down-then-recover
SEQUENCE is invisible. At intraday resolution the sequence is observable, so the mechanism
can be tested directly rather than inferred.

What is measured
----------------
For each intraday bar, against a level formed from PRIOR bars only:
  * Sweep:      bar low pierces the level but the close is back at/above it
  * Reversion:  does price trade back above the level within N bars?
  * Forward:    return over N bars after the sweep vs the UNCONDITIONAL return over the
                same horizon (the correct null -- gold drifts, so comparing to zero would
                manufacture edge)
  * Volume:     is the sweep bar's volume elevated vs its own trailing median?
  * Clock:      do sweeps cluster at particular session hours?

Guardrails carried over from the daily work (both were live defects once):
  1. Null is the unconditional forward return, never zero.
  2. Exits are never resolved on the entry bar (no intrabar foresight).
  3. Levels use prior bars only (no look-ahead).
  4. No synthesis: raises if real intraday data is unavailable.
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

SESSIONS = {
    "asia": (19, 24),
    "london_open": (2, 5),
    "ny_open": (8, 11),
    "ny_pm": (13, 16),
}


class IntradayDataUnavailable(RuntimeError):
    """Real intraday bars could not be obtained; the study must not be simulated."""


class IntradaySweepStudy:
    def __init__(self, ticker="GC=F", interval="1h", period="730d"):
        self.ticker = ticker
        self.interval = interval
        self.period = period
        self.cache = os.path.join(HISTORY_DIR, f"intraday_{ticker.replace('=', '_')}_{interval}.csv")

    # --- data ---------------------------------------------------------------------
    def load(self, refresh=False):
        if not refresh and os.path.exists(self.cache) and os.path.getsize(self.cache) > 0:
            df = pd.read_csv(self.cache)
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
            df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
            if len(df) > 100:
                return df

        raw = yf.download(self.ticker, interval=self.interval, period=self.period,
                          progress=False, auto_adjust=False)
        if raw is None or raw.empty:
            raise IntradayDataUnavailable(f"no intraday bars for {self.ticker} @ {self.interval}")
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        missing = [c for c in ("High", "Low", "Close", "Volume") if c not in raw.columns]
        if missing:
            raise IntradayDataUnavailable(f"intraday payload missing {missing}")

        df = raw.reset_index()
        ts_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
        df = df.rename(columns={ts_col: "timestamp", "High": "high", "Low": "low",
                                "Close": "close", "Open": "open", "Volume": "volume"})
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        df = df.dropna(subset=["timestamp", "high", "low", "close"]).sort_values("timestamp")
        df = df[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
        if len(df) < 200:
            raise IntradayDataUnavailable(f"intraday history too short: {len(df)} bars")

        os.makedirs(HISTORY_DIR, exist_ok=True)
        df.to_csv(self.cache, index=False)
        return df

    # --- level construction (prior bars only) --------------------------------------
    @staticmethod
    def add_levels(df, lookback=12):
        out = df.copy()
        # Rolling swing low over PRIOR bars (shift 1 => never includes the current bar).
        out["level_rolling"] = out["low"].shift(1).rolling(lookback).min()
        # Prior session low: the low of the previous UTC calendar day.
        day = out["timestamp"].dt.floor("D")
        daily_low = out.groupby(day)["low"].min()
        out["level_prior_day"] = day.map(daily_low.shift(1))
        return out

    # --- sweep detection -----------------------------------------------------------
    @staticmethod
    def detect_sweeps(df, level_col, min_pierce_bps=1.0, require_close_back=True):
        """A sweep pierces `level_col` and closes back at/above it.

        The close-back condition is the observable signature: liquidity was taken but
        price was rejected. Without it we would only be measuring downside continuation.
        """
        out = df.copy()
        lvl = out[level_col]
        pierce = out["low"] < lvl
        depth_bps = (lvl - out["low"]) / lvl * 10000.0
        deep_enough = depth_bps >= min_pierce_bps
        if require_close_back:
            out["is_sweep"] = pierce & deep_enough & (out["close"] >= lvl)
        else:
            out["is_sweep"] = pierce & deep_enough
        out["pierce_depth_bps"] = depth_bps
        rng = (out["high"] - out["low"]).replace(0, np.nan)
        out["lower_wick_frac"] = (np.minimum(out["close"], out["open"]) - out["low"]) / rng
        return out

    # --- forward outcomes ----------------------------------------------------------
    @staticmethod
    def forward_returns(df, horizon):
        close = df["close"].to_numpy()
        fwd = np.full(len(close), np.nan)
        fwd[:-horizon] = (close[horizon:] - close[:-horizon]) / close[:-horizon]
        return fwd

    @staticmethod
    def _welch(a, b):
        a = np.asarray(a)[~np.isnan(np.asarray(a, dtype=float))]
        b = np.asarray(b)[~np.isnan(np.asarray(b, dtype=float))]
        if len(a) < 2 or len(b) < 2:
            return float("nan")
        se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
        return float((a.mean() - b.mean()) / se) if se > 0 else float("nan")

    # --- the study -----------------------------------------------------------------
    def evaluate(self, level_col="level_rolling", horizons=(1, 2, 4, 8, 12),
                 min_pierce_bps=1.0):
        df = self.add_levels(self.load())
        df = self.detect_sweeps(df, level_col, min_pierce_bps=min_pierce_bps)
        df = df.dropna(subset=[level_col]).reset_index(drop=True)


        lvl = df[level_col].to_numpy()
        close = df["close"].to_numpy()
        sweep_idx = np.where(df["is_sweep"].to_numpy())[0]

        rows = []
        for h in horizons:
            fwd = self.forward_returns(df, h)
            sweep_fwd = fwd[sweep_idx]
            all_fwd = fwd[~np.isnan(fwd)]

            # Reversion: does price trade back above the level within h bars, ignoring the
            # entry bar itself (no intrabar foresight)?
            hit = [int(np.nanmax(close[i + 1:min(i + 1 + h, len(df))]) >= lvl[i])
                   for i in sweep_idx if i + 1 < len(df)]
            reversion_pct = float(np.mean(hit) * 100) if hit else float("nan")

            rows.append({
                "level": level_col,
                "horizon_bars": h,
                "sweeps": int(len(sweep_idx)),
                "reversion_pct": round(reversion_pct, 2),
                "sweep_fwd_pct": round(float(np.nanmean(sweep_fwd) * 100), 4) if sweep_fwd.size else None,
                "uncond_fwd_pct": round(float(np.nanmean(all_fwd) * 100), 4),
                "excess_pct": round(float((np.nanmean(sweep_fwd) - np.nanmean(all_fwd)) * 100), 4)
                if sweep_fwd.size else None,
                "t_vs_baseline": round(self._welch(sweep_fwd, all_fwd), 3),
            })
        return pd.DataFrame(rows)

    # --- the decisive contrast ------------------------------------------------------
    def sweep_vs_breakdown(self, level_col="level_rolling", horizons=(1, 2, 4, 8, 12),
                           min_pierce_bps=1.0):
        """The test that isolates the mechanism.

        A high "reversion rate" is meaningless on its own: gold drifts up, so price
        regains almost any level within N bars sooner or later. The question is whether
        REJECTION matters. That requires three matched groups:

            no_pierce   price never traded below the level
            sweep       pierced, then closed back ABOVE  (liquidity taken, rejected)
            breakdown   pierced, then closed BELOW       (liquidity taken, accepted)

        If the stop-hunt thesis is right, 'sweep' must outperform 'breakdown'. If the two
        are indistinguishable, the rejection is not informative.
        """
        df = self.add_levels(self.load())
        df = self.detect_sweeps(df, level_col, min_pierce_bps=min_pierce_bps)
        df = df.dropna(subset=[level_col]).reset_index(drop=True)

        lvl = df[level_col]
        pierced = df["low"] < lvl
        depth_ok = ((lvl - df["low"]) / lvl * 10000.0) >= min_pierce_bps
        active = pierced & depth_ok
        closed_back = df["close"] >= lvl

        groups = {
            "no_pierce": (~active).to_numpy(),
            "sweep": (active & closed_back).to_numpy(),
            "breakdown": (active & ~closed_back).to_numpy(),
        }






        rows = []
        for h in horizons:
            fwd = self.forward_returns(df, h)
            row = {"level": level_col, "horizon_bars": h}
            for name, mask in groups.items():
                vals = fwd[mask]
                vals = vals[~np.isnan(vals)]
                row[f"{name}_n"] = int(mask.sum())
                row[f"{name}_fwd_pct"] = round(float(vals.mean() * 100), 4) if vals.size else None
            sw = fwd[groups["sweep"]]
            bd = fwd[groups["breakdown"]]
            row["sweep_minus_breakdown_pct"] = (
                round(float((np.nanmean(sw) - np.nanmean(bd)) * 100), 4)
                if np.any(~np.isnan(sw)) and np.any(~np.isnan(bd)) else None)
            row["t_sweep_vs_breakdown"] = round(self._welch(sw, bd), 3)
            rows.append(row)
        return pd.DataFrame(rows)

    # --- supporting structure ------------------------------------------------------
    def volume_signature(self, level_col="level_rolling", min_pierce_bps=1.0):
        """Is sweep-bar volume elevated? Real liquidity-taking should show a spike."""
        df = self.detect_sweeps(self.add_levels(self.load()), level_col, min_pierce_bps)
        df["vol_med"] = df["volume"].shift(1).rolling(50, min_periods=20).median()
        df["vol_ratio"] = df["volume"] / df["vol_med"].replace(0, np.nan)
        sweeps = df.loc[df["is_sweep"], "vol_ratio"].dropna()
        others = df.loc[~df["is_sweep"], "vol_ratio"].dropna()
        return {
            "sweep_mean_vol_ratio": round(float(sweeps.mean()), 3) if len(sweeps) else None,
            "non_sweep_mean_vol_ratio": round(float(others.mean()), 3),
            "t_vs_non_sweep": round(self._welch(sweeps.to_numpy(), others.to_numpy()), 3),
            "sweep_volume_elevated": bool(len(sweeps) > 10 and sweeps.mean() > others.mean() * 1.05),
        }

    def clock_signature(self, level_col="level_rolling", min_pierce_bps=1.0):
        """Do sweeps cluster at particular session hours?"""
        df = self.detect_sweeps(self.add_levels(self.load()), level_col, min_pierce_bps)
        df = df.dropna(subset=["is_sweep"]).copy()
        df["hour"] = df["timestamp"].dt.hour
        by_hour = df.groupby("hour")["is_sweep"].agg(["sum", "count"])
        by_hour["rate_pct"] = (by_hour["sum"] / by_hour["count"] * 100).round(2)
        session_rates = {}
        for name, (start, end) in SESSIONS.items():
            mask = df["hour"].between(start, end - 1)
            subset = df[mask]
            session_rates[name] = round(float(subset["is_sweep"].mean() * 100), 2) if len(subset) else None
        return {
            "top_hours_by_sweep_rate": by_hour.sort_values("rate_pct", ascending=False)["rate_pct"].head(6).to_dict(),
            "session_sweep_rate_pct": session_rates,
        }

    def run(self):
        print(f"🔬 [MICROSTRUCTURE] {self.ticker} @ {self.interval} — sweep thesis at intraday resolution")
        df = self.load()
        print(f"    bars: {len(df):,}  {df['timestamp'].min()} → {df['timestamp'].max()}\n")

        results = {}
        for level in ("level_rolling", "level_prior_day"):
            print(f"--- sweeps of {level} (close-back-above required) ---")
            table = self.evaluate(level_col=level)
            print(table.to_string(index=False))
            print()
            results[level] = table

        print("--- DECISIVE: does REJECTION matter? sweep (pierce+recover) vs breakdown (pierce+close below) ---")
        for level in ("level_rolling", "level_prior_day"):
            print(f"  [{level}]")
            print(self.sweep_vs_breakdown(level_col=level).to_string(index=False))
            print()

        print("--- volume signature (is liquidity being taken?) ---")
        for k, v in self.volume_signature().items():
            print(f"    {k}: {v}")
        print()
        print("--- clock signature (do sweeps cluster by session?) ---")
        clock = self.clock_signature()
        print(f"    top hours by sweep rate: {clock['top_hours_by_sweep_rate']}")
        print(f"    sweep rate by session:   {clock['session_sweep_rate_pct']}")
        return results


def main():
    study = IntradaySweepStudy()
    try:
        results = study.run()
    except IntradayDataUnavailable as exc:
        print(f"🔴 [MICROSTRUCTURE ABORTED] {exc}")
        print("    No result is reported rather than a simulated one.")
        return 1

    best = max((r["t_vs_baseline"].abs().max() for r in results.values() if len(r)),
               default=float("nan"))
    print()
    if best == best and best >= 2.0:
        print(f"⚠️  A horizon reached |t| >= 2 (best {best}). Candidate worth out-of-sample "
              f"confirmation before it means anything.")
    else:
        print(f"🏁 No horizon reached |t| >= 2 (best {best}). Even at the resolution where the "
              f"mechanism operates, the sweep does not predict a reversion.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
