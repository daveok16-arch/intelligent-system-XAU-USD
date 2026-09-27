"""
SYSTEM MODULE: WALK-FORWARD ROBUSTNESS TEST
ROLE: DISTINGUISH A ROBUST EFFECT FROM PERIOD-SPECIFIC LUCK

Why a single 70/30 split is not enough
--------------------------------------
The conditional studies found a short-horizon volatility effect that was strong in
discovery and weak in validation. Two explanations fit that pattern equally well:

  (a) OVERFITTING - the effect is spurious and simply decayed as expected
  (b) REGIME      - the effect is real but the validation period had different market
                    character, so a single split mislabels regime change as decay

A single split cannot tell these apart. A walk-forward across MULTIPLE folds can:
if the effect appears in several independent windows, it is not a one-off; if it appears
in one window only, it was luck. This is the standard remedy, and it is the honest way to
test a surviving signal rather than reporting the one split that looked best.

Also measured here: ECONOMIC MAGNITUDE. A 20% relative increase in volatility is
statistically interesting but may be untradeable. Fraction-of-a-percent hour ranges are
reported so the effect can be judged in real terms, not just t-statistics.
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
N_FOLDS = int(os.getenv("MS_FOLDS", "5"))
MIN_FOLD_BARS = int(os.getenv("MS_MIN_FOLD_BARS", "1200"))


class WalkForwardStudy:
    def __init__(self, base=None):
        self.base = base or IntradaySweepStudy()

    def build(self, level_col="level_rolling", min_pierce_bps=1.0):
        df = self.base.add_levels(self.base.load())
        df = self.base.detect_sweeps(df, level_col, min_pierce_bps=min_pierce_bps)
        df = df.dropna(subset=[level_col]).reset_index(drop=True)
        df["vol_med"] = df["volume"].shift(1).rolling(50, min_periods=20).median()
        df["vol_ratio"] = df["volume"] / df["vol_med"].replace(0, np.nan)
        df["vol_elevated"] = df["vol_ratio"] >= VOLUME_ELEVATED_RATIO
        return df[df["vol_ratio"].notna()].reset_index(drop=True)

    @staticmethod
    def abs_move(df, horizon=1):
        close = df["close"].to_numpy()
        out = np.full(len(close), np.nan)
        out[:-horizon] = np.abs(close[horizon:] - close[:-horizon]) / close[:-horizon]
        return out

    @staticmethod
    def _welch(a, b):
        a = np.asarray(a, dtype=float)[~np.isnan(np.asarray(a, dtype=float))]
        b = np.asarray(b, dtype=float)[~np.isnan(np.asarray(b, dtype=float))]
        if len(a) < 2 or len(b) < 2:
            return float("nan")
        se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
        return float((a.mean() - b.mean()) / se) if se > 0 else float("nan")

    def fold_table(self, df, level_col, mask_name, mask):
        """One row per fold, so stability across windows is visible at a glance."""
        am = self.abs_move(df)
        rows = []
        for k in range(N_FOLDS):
            lo = int(len(df) * k / N_FOLDS)
            hi = int(len(df) * (k + 1) / N_FOLDS)
            seg = slice(lo, hi)
            if hi - lo < MIN_FOLD_BARS:
                continue
            seg_mask = mask.iloc[seg].to_numpy()
            seg_am = am[seg]
            sub = seg_am[seg_mask]
            sub = sub[~np.isnan(sub)]
            base = seg_am[~np.isnan(seg_am)]
            if len(sub) < 15:
                continue
            rows.append({
                "fold": k + 1,
                "period": f"{df['timestamp'].iloc[lo].date()}→{df['timestamp'].iloc[hi-1].date()}",
                "n_sweeps": int(len(sub)),
                "sweep_absmove_pct": round(float(sub.mean()) * 100, 4),
                "uncond_absmove_pct": round(float(base.mean()) * 100, 4),
                "ratio": round(float(sub.mean() / base.mean()), 3) if base.mean() else None,
                "t": round(float(self._welch(sub, base)), 3),
            })
        return pd.DataFrame(rows)

    def run(self):
        print(f"🧭 [WALK-FORWARD] {self.base.ticker} @ {self.base.interval}, {N_FOLDS} folds")
        print("    Question: is the short-horizon volatility effect stable, or one window's luck?\n")

        summary = {}
        for level in ("level_rolling", "level_prior_day"):
            df = self.build(level_col=level)
            print(f"=== {level} :: all sweeps ===")
            t_all = self.fold_table(df, level, "all", df["is_sweep"])
            print(t_all.to_string(index=False))
            print()

            print(f"=== {level} :: HIGH-VOLUME sweeps only ===")
            t_hi = self.fold_table(df, level, "highvol", df["is_sweep"] & df["vol_elevated"])
            print(t_hi.to_string(index=False))
            print()

            for name, tbl in (("all", t_all), ("highvol", t_hi)):
                if len(tbl):
                    pos = int((tbl["t"] >= 2.0).sum())
                    neg = int((tbl["t"] <= -2.0).sum())
                    summary[f"{level}/{name}"] = {
                        "folds": len(tbl),
                        "significant_positive": pos,
                        "significant_negative": neg,
                        "median_ratio": round(float(tbl["ratio"].median()), 3),
                        "median_t": round(float(tbl["t"].median()), 3),
                    }

            print(f"--- economic magnitude ({level}) ---")
            print(f"    median sweep abs-move: {float(t_all['sweep_absmove_pct'].median()):.4f}% of price")
            print(f"    median unconditional:  {float(t_all['uncond_absmove_pct'].median()):.4f}% of price")
            print(f"    median ratio:          {float(t_all['ratio'].median()):.3f}x\n")

        print("=== STABILITY SUMMARY ===")
        for k, v in summary.items():
            print(f"  {k:<28} folds={v['folds']}  sig+ ={v['significant_positive']}  "
                  f"sig-={v['significant_negative']}  median ratio={v['median_ratio']}  "
                  f"median t={v['median_t']}")
        return summary


def main():
    study = WalkForwardStudy()
    try:
        study.run()
    except IntradayDataUnavailable as exc:
        print(f"🔴 [WALK-FORWARD ABORTED] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
