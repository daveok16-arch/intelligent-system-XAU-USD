"""
SYSTEM MODULE: SWEEP VOLATILITY HYPOTHESIS
ROLE: TEST WHETHER A SWEEP PREDICTS MAGNITUDE RATHER THAN DIRECTION

The finding that motivates this module
--------------------------------------
Across three daily backtests and a disciplined intraday study, sweeps showed:

    * a strong VOLUME signature      2.14x trailing median volume, t = 13.90
    * strong SESSION clustering      NY afternoon 11.47% vs Asia 3.74%
    * NO directional edge            zero pre-registered directional hypotheses passed,
                                     and discovery hits failed out-of-sample

Detection and timing are predictable; direction is not. That pattern is exactly what a
LIQUIDITY EVENT looks like: a discrete release of resting orders that resolves the local
order book, after which price moves -- but in a direction the event itself does not
determine.

So the untested implication is: **does a sweep predict the SIZE of the subsequent move,
even though it does not predict its direction?**

This matters because volatility is separately harvestable (options, vol targeting,
position sizing, stop placement) without taking a directional view.

Pre-registered hypotheses
-------------------------
  V1  Absolute forward return after a sweep exceeds the unconditional absolute forward
      return over the same horizon.
  V2  High-volume sweeps (>1.5x trailing median) show a larger volatility expansion than
      low-volume sweeps.
  V3  Realized range over the next N bars is greater after a sweep than unconditionally.

Controls: discovery on the first 70%, validation on the last 30%; |t|>=2 AND
pre-registered sign required; expected false positives reported; prior bars only for all
features; the null is the unconditional distribution over the same horizon.
"""

import os
import sys

import numpy as np
import pandas as pd

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from engine_microstructure import IntradaySweepStudy, IntradayDataUnavailable  # noqa: E402

VOLUME_ELEVATED_RATIO = float(os.getenv("MS_VOLUME_RATIO", "1.5"))
HORIZONS = (1, 2, 4, 8, 12)
DISCOVERY_FRACTION = 0.70


class SweepVolatilityStudy:
    def __init__(self, base=None):
        self.base = base or IntradaySweepStudy()

    # --- features ------------------------------------------------------------------
    def build(self, level_col="level_rolling", min_pierce_bps=1.0):
        df = self.base.add_levels(self.base.load())
        df = self.base.detect_sweeps(df, level_col, min_pierce_bps=min_pierce_bps)
        df = df.dropna(subset=[level_col]).reset_index(drop=True)
        df["vol_med"] = df["volume"].shift(1).rolling(50, min_periods=20).median()
        df["vol_ratio"] = df["volume"] / df["vol_med"].replace(0, np.nan)
        df["vol_elevated"] = df["vol_ratio"] >= VOLUME_ELEVATED_RATIO
        return df[df["vol_ratio"].notna()].reset_index(drop=True)

    # --- magnitude measures --------------------------------------------------------
    @staticmethod
    def abs_forward_return(df, horizon):
        """Absolute forward return: unsigned move size."""
        close = df["close"].to_numpy()
        fwd = np.full(len(close), np.nan)
        fwd[:-horizon] = np.abs(close[horizon:] - close[:-horizon]) / close[:-horizon]
        return fwd

    @staticmethod
    def forward_range(df, horizon):
        """High-low range over the next `horizon` bars, as a fraction of price."""
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        close = df["close"].to_numpy()
        n = len(df)
        out = np.full(n, np.nan)
        for i in range(n - horizon):
            window_h = high[i + 1:i + 1 + horizon]
            window_l = low[i + 1:i + 1 + horizon]
            base = close[i]
            if len(window_h) and base:
                out[i] = (window_h.max() - window_l.min()) / base
        return out

    @staticmethod
    def _welch(a, b):
        a = np.asarray(a, dtype=float)[~np.isnan(np.asarray(a, dtype=float))]
        b = np.asarray(b, dtype=float)[~np.isnan(np.asarray(b, dtype=float))]
        if len(a) < 2 or len(b) < 2:
            return float("nan")
        se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
        return float((a.mean() - b.mean()) / se) if se > 0 else float("nan")

    def _cell(self, name, measures_fn, df, mask, horizon):
        """Higher-is-larger is the pre-registered direction for every volatility cell."""
        full = measures_fn(df, horizon)
        sub = full[mask.to_numpy()]
        sub = sub[~np.isnan(sub)]
        base = full[~np.isnan(full)]
        if len(sub) < 20:
            return {"hypothesis": name, "horizon": horizon, "n": int(len(sub)),
                    "note": "n<20", "t": None, "excess": None, "ratio": None}
        excess = float(sub.mean() - base.mean())
        return {
            "hypothesis": name,
            "horizon": horizon,
            "n": int(len(sub)),
            "sweep": round(float(sub.mean()) * 100, 4),
            "uncond": round(float(base.mean()) * 100, 4),
            "excess": round(excess * 100, 4),
            "ratio": round(float(sub.mean() / base.mean()), 3) if base.mean() else None,
            "t": round(float(self._welch(sub, base)), 3),
        }

    def battery(self, df, level_col):
        sweeps = df["is_sweep"]
        rows = []
        for h in HORIZONS:
            rows.append(self._cell("V1_sweep_absmove", self.abs_forward_return, df, sweeps, h))
            rows.append(self._cell("V2_highvol_absmove", self.abs_forward_return, df,
                                   sweeps & df["vol_elevated"], h))
            rows.append(self._cell("V2_lowvol_absmove", self.abs_forward_return, df,
                                   sweeps & ~df["vol_elevated"], h))
            rows.append(self._cell("V3_sweep_range", self.forward_range, df, sweeps, h))
        return pd.DataFrame(rows)

    def run(self, level_col="level_rolling"):
        full = self.build(level_col=level_col)
        split = int(len(full) * DISCOVERY_FRACTION)
        disc = full.iloc[:split].reset_index(drop=True)
        valid = full.iloc[split:].reset_index(drop=True)

        print(f"📈 [VOLATILITY STUDY] {self.base.ticker} @ {self.base.interval} :: {level_col}")
        print(f"    bars {len(full):,} | discovery {len(disc):,} | validation {len(valid):,}")
        print("    pre-registered: V1 sweep->magnitude, V2 volume conditioning, V3 sweep->range\n")

        d = self.battery(disc, level_col)
        v = self.battery(valid, level_col)

        n_cells = int(d["t"].notna().sum())
        print(f"--- DISCOVERY --- cells {n_cells} | expected FP at |t|>2: {n_cells*0.05:.1f}")
        sig_d = d[(d["t"].notna()) & (d["t"] >= 2.0)]
        print(f"    passing (|t|>=2, positive sign as pre-registered): {len(sig_d)}")
        if len(sig_d):
            print(sig_d[["hypothesis", "horizon", "n", "sweep", "uncond", "ratio", "t"]].to_string(index=False))
        print()

        n_cells_v = int(v["t"].notna().sum())
        print(f"--- VALIDATION (the evidence that counts) --- cells {n_cells_v} | "
              f"expected FP at |t|>2: {n_cells_v*0.05:.1f}")
        sig_v = v[(v["t"].notna()) & (v["t"] >= 2.0)]
        print(f"    passing (|t|>=2, positive sign): {len(sig_v)}")
        if len(sig_v):
            print(sig_v[["hypothesis", "horizon", "n", "sweep", "uncond", "ratio", "t"]].to_string(index=False))
        print()

        survivors = set(zip(sig_d["hypothesis"], sig_d["horizon"])) if len(sig_d) else set()
        if survivors:
            print("--- SURVIVOR CHECK (discovery cells re-tested out-of-sample) ---")
            vi = v.set_index(["hypothesis", "horizon"])
            di = d.set_index(["hypothesis", "horizon"])
            confirmed = 0
            for key in sorted(survivors):
                if key in vi.index:
                    rt = vi.loc[key, "t"]
                    ok = bool(pd.notna(rt) and rt >= 2.0)
                    confirmed += ok
                    dt = di.loc[key, "t"]
                    print(f"    {key[0]:<18} h={key[1]:<2}  disc t={dt:+.2f}  "
                          f"valid t={rt if pd.notna(rt) else float('nan'):+.2f}  "
                          f"{'CONFIRMED' if ok else 'not confirmed'}")
            print(f"    confirmed: {confirmed}/{len(survivors)}")
        else:
            print("--- SURVIVOR CHECK: nothing passed discovery ---")
        print()
        return {"discovery": d, "validation": v}


def main():
    study = SweepVolatilityStudy()
    try:
        for level in ("level_rolling", "level_prior_day"):
            study.run(level_col=level)
    except IntradayDataUnavailable as exc:
        print(f"🔴 [VOLATILITY STUDY ABORTED] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
