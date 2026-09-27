"""
SYSTEM MODULE: SWEEP SPECIFICITY CONTROL
ROLE: RULE OUT THE LEVERAGE EFFECT AS THE EXPLANATION

The problem this solves
-----------------------
The sweep volatility effect replicated across gold, silver, platinum and copper
(1.18x-1.34x, all t >= 3.5). That kills the "one series artefact" explanation. It does
NOT kill a more fundamental one:

    Volatility is higher after negative returns. This is the leverage effect -- one of the
    most robust facts in finance. If a "sweep" is merely a proxy for "price recently fell",
    then the entire finding is a rediscovery of a textbook result, and the sweep
    definition adds nothing.

That is the correct null, and it is stricter than the unconditional baseline. A sweep must
outperform a MATCHED ORDINARY DIP, not merely the average bar.

Controls, in increasing strictness
----------------------------------
  C1 unconditional        all bars (the baseline used so far)
  C2 down bars            close < previous close  (any decline)
  C3 matched dips         down bars whose decline magnitude matches the sweep's decline
                          (decile-matched, so size of move cannot explain the difference)
  C4 near-miss dips       pierced the same level but did NOT close back above it
                          (same structure, opposite resolution)

If sweeps beat C1 but not C2-C4, the effect is the leverage effect and the sweep label is
decorative. If sweeps beat C3 in particular, the rejection structure carries information
beyond the size of the prior decline.

Also reported: per-asset sub-period stability, to address the shared-window concern (all
assets live in the same two years, so a common volatility regime could drive all of them).
"""

import os
import sys

import numpy as np
import pandas as pd

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from microstructure_crossasset import CrossAssetStudy, ASSETS, ReplicationDataUnavailable  # noqa: E402

N_MATCH_BINS = int(os.getenv("MS_MATCH_BINS", "10"))
N_FOLDS = int(os.getenv("MS_FOLDS", "4"))


class SweepSpecificityStudy:
    def __init__(self, base=None):
        self.base = base or CrossAssetStudy()

    @staticmethod
    def welch(a, b):
        return CrossAssetStudy.welch(a, b)

    def features(self, symbol):
        df = self.base.build(self.base.load(symbol))
        am = self.base.abs_move(df, 1)
        out = df.copy()
        out["abs_fwd"] = am
        # Magnitude of the prior bar's move -- the leverage-effect driver.
        out["prior_move"] = (out["close"] - out["close"].shift(1)).abs() / out["close"].shift(1)
        out["is_down"] = out["close"] < out["close"].shift(1)
        # Did it pierce the level at all (regardless of how it closed)?
        out["pierced"] = out["low"] < out["level"]
        out = out.dropna(subset=["abs_fwd", "prior_move"]).reset_index(drop=True)
        return out

    def controls(self, symbol):
        df = self.features(symbol)
        sweep = df["is_sweep"]
        baseline = df["abs_fwd"].to_numpy()
        baseline = baseline[~np.isnan(baseline)]
        sweep_vals = df.loc[sweep, "abs_fwd"].dropna()

        # C3: decile-match on the magnitude of the PRIOR move, so the sweep group is
        # compared against ordinary dips of the same size.
        df["_bin"] = pd.qcut(df["prior_move"], N_MATCH_BINS, labels=False, duplicates="drop")
        sweep_bins = df.loc[sweep, "_bin"].dropna()
        matched = df[(~sweep) & df["is_down"] & df["_bin"].isin(set(sweep_bins.unique()))]
        matched_vals = matched["abs_fwd"].dropna()

        out = {
            "symbol": symbol, "asset": ASSETS.get(symbol, symbol),
            "n_sweep": int(len(sweep_vals)),
            "sweep_mean": round(float(sweep_vals.mean()) * 100, 4),
            "C1_uncond_mean": round(float(baseline.mean()) * 100, 4),
            "C1_t": round(self.welch(sweep_vals.to_numpy(), baseline), 3),
        }
        down = df.loc[df["is_down"], "abs_fwd"].dropna()
        out["C2_down_mean"] = round(float(down.mean()) * 100, 4)
        out["C2_t"] = round(self.welch(sweep_vals.to_numpy(), down.to_numpy()), 3)

        out["C3_matched_n"] = int(len(matched_vals))
        out["C3_matched_mean"] = round(float(matched_vals.mean()) * 100, 4) if len(matched_vals) else None
        out["C3_t"] = round(self.welch(sweep_vals.to_numpy(), matched_vals.to_numpy()), 3) \
            if len(matched_vals) >= 20 else None

        nmi = df[(df["pierced"]) & (~sweep) & df["is_down"]]
        nmi_vals = nmi["abs_fwd"].dropna()
        out["C4_nearmiss_n"] = int(len(nmi_vals))
        out["C4_nearmiss_mean"] = round(float(nmi_vals.mean()) * 100, 4) if len(nmi_vals) else None
        out["C4_t"] = round(self.welch(sweep_vals.to_numpy(), nmi_vals.to_numpy()), 3) \
            if len(nmi_vals) >= 20 else None
        return out

    def fold_stability(self, symbol):
        """Does the effect hold in sub-periods, or is it one volatility regime?"""
        df = self.features(symbol)
        rows = []
        for k in range(N_FOLDS):
            lo, hi = int(len(df) * k / N_FOLDS), int(len(df) * (k + 1) / N_FOLDS)
            seg = df.iloc[lo:hi]
            sv = seg.loc[seg["is_sweep"], "abs_fwd"].dropna()
            bv = seg["abs_fwd"].dropna()
            if len(sv) < 20 or len(bv) < 50:
                continue
            rows.append({"fold": k + 1, "n": int(len(sv)),
                         "ratio": round(float(sv.mean() / bv.mean()), 3),
                         "t": round(self.welch(sv.to_numpy(), bv.to_numpy()), 3)})
        return pd.DataFrame(rows)

    def run(self):
        print("🔎 [SPECIFICITY CONTROL] is the sweep effect more than the leverage effect?")
        print("    C1 unconditional | C2 down bars | C3 size-matched dips | C4 pierced-but-not-rejected\n")
        rows = []
        for sym in ASSETS:
            try:
                rows.append(self.controls(sym))
            except ReplicationDataUnavailable as exc:
                print(f"    ⚠️ {sym}: {exc}")
        df = pd.DataFrame(rows)
        cols = ["asset", "n_sweep", "C1_uncond_mean", "C1_t", "C2_down_mean", "C2_t",
                "C3_matched_n", "C3_matched_mean", "C3_t", "C4_nearmiss_n", "C4_nearmiss_mean", "C4_t"]
        print(df[[c for c in cols if c in df.columns]].to_string(index=False))
        print()

        print("--- VERDICT ---")
        survived = df["C3_t"].dropna()
        beat_c3 = int((survived >= 2.0).sum()) if len(survived) else 0
        beat_c2 = int((df["C2_t"].dropna() >= 2.0).sum())
        print(f"    beats unconditional baseline (C1): {int((df['C1_t'] >= 2.0).sum())}/4")
        print(f"    beats any ordinary down bar   (C2): {beat_c2}/4")
        print(f"    beats size-matched dip        (C3): {beat_c3}/{len(survived) if len(survived) else 0}")
        if beat_c3 >= 3:
            print("    ✅ The rejection structure carries information BEYOND the size of the prior decline.")
        elif beat_c2 >= 3:
            print("    ◻️  Sweeps beat ordinary dips but not size-matched dips -- the effect is")
            print("       largely explained by move magnitude (leverage effect).")
        else:
            print("    ❌ Sweeps do not beat ordinary down bars. The finding is the leverage effect.")
        print()

        print("--- SUB-PERIOD STABILITY (shared-window concern) ---")
        for sym in ASSETS:
            try:
                t = self.fold_stability(sym)
                if len(t):
                    pos = int((t["ratio"] > 1.0).sum())
                    print(f"    {ASSETS[sym]:<9} folds>1.0: {pos}/{len(t)}  "
                          f"ratios: {', '.join(f'{r:.3f}' for r in t['ratio'])}")
            except ReplicationDataUnavailable:
                pass
        return df


def main():
    study = SweepSpecificityStudy()
    try:
        study.run()
    except ReplicationDataUnavailable as exc:
        print(f"🔴 [SPECIFICITY CONTROL ABORTED] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
