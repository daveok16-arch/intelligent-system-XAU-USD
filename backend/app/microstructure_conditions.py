"""
SYSTEM MODULE: CONDITIONAL MICROSTRUCTURE STUDY
ROLE: TEST PRE-REGISTERED CONDITIONS ON THE SWEEP SIGNAL, WITH OUT-OF-SAMPLE VALIDATION

Context
-------
The aggregate intraday test found no tradeable reversion, but it also found two real
structural facts that an aggregate test averages away:

    * sweep bars carry 2.14x trailing-median volume (non-sweep: 1.34x, t=13.9)
    * sweeps cluster by session: NY afternoon 11.47%, Asia 3.74%

This module asks whether those conditions MODULATE the signal -- i.e. whether a subset
of sweeps carries information that the pooled test could not see.

Method discipline (this is the part that matters)
-------------------------------------------------
Slicing by condition x horizon generates many cells, and searching many cells guarantees
false positives. So, BEFORE looking at any result:

  H1 VOLUME      sweeps with elevated volume (>1.5x trailing median) show positive
                 forward return vs the unconditional baseline; low-volume sweeps do not.
  H2 SESSION     sweeps during NY active hours (13-20 UTC) revert; Asia-hour sweeps do not.
  H3 DEPTH       deeper pierces (larger penetration below the level, in bps) revert more
                 strongly than shallow ones.
  H4 COMBINED    NY-active + elevated-volume sweeps are the strongest subset.

Controls applied:
  * OUT-OF-SAMPLE: discovery on the first 70% of bars, validation on the last 30%. Only
    validation results are treated as evidence; discovery is exploratory by construction.
  * MULTIPLE TESTING: the number of cells examined is reported alongside the expected
    false-positive count, so a lone |t|>2 is not mistaken for a finding.
  * DIRECTION: each hypothesis states its expected sign in advance; a flipped sign is a
    refutation, not a rediscovery.
  * Null is the unconditional forward return over the same horizon (never zero).
  * Levels use prior bars only; exits never resolve on the entry bar.
"""

import os
import sys

import numpy as np
import pandas as pd

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from engine_microstructure import IntradaySweepStudy, IntradayDataUnavailable  # noqa: E402

# --- pre-registered configuration -------------------------------------------------
VOLUME_ELEVATED_RATIO = float(os.getenv("MS_VOLUME_RATIO", "1.5"))
NY_START_HOUR, NY_END_HOUR = 13, 20          # UTC, NY active session
DEPTH_SPLIT_BPS = float(os.getenv("MS_DEPTH_BPS", "10.0"))
HORIZONS = (1, 2, 4, 8)
DISCOVERY_FRACTION = 0.70


class ConditionalMicrostructureStudy:
    def __init__(self, base=None):
        self.base = base or IntradaySweepStudy()

    # --- feature construction -----------------------------------------------------
    def build(self, level_col="level_rolling", min_pierce_bps=1.0):
        df = self.base.add_levels(self.base.load())
        df = self.base.detect_sweeps(df, level_col, min_pierce_bps=min_pierce_bps)
        df = df.dropna(subset=[level_col]).reset_index(drop=True)

        # Volume context from PRIOR bars only.
        df["vol_med"] = df["volume"].shift(1).rolling(50, min_periods=20).median()
        df["vol_ratio"] = df["volume"] / df["vol_med"].replace(0, np.nan)

        df["hour"] = df["timestamp"].dt.hour
        df["is_ny"] = df["hour"].between(NY_START_HOUR, NY_END_HOUR - 1)
        df["vol_elevated"] = df["vol_ratio"] >= VOLUME_ELEVATED_RATIO
        df["deep_pierce"] = df["pierce_depth_bps"] >= DEPTH_SPLIT_BPS
        df["level_col"] = level_col
        return df[df["vol_ratio"].notna()].reset_index(drop=True)

    @staticmethod
    def unconditional(df, horizon):
        fwd = IntradaySweepStudy.forward_returns(df, horizon)
        return fwd[~np.isnan(fwd)]

    @staticmethod
    def group_forward(df, mask, horizon):
        fwd = IntradaySweepStudy.forward_returns(df, horizon)
        vals = fwd[mask.to_numpy()]
        return vals[~np.isnan(vals)]

    # --- one hypothesis evaluation -------------------------------------------------
    def evaluate_cell(self, df, name, mask, horizon, expected_sign):
        baseline = self.unconditional(df, horizon)
        vals = self.group_forward(df, mask, horizon)
        if len(vals) < 20:
            return {"hypothesis": name, "horizon": horizon, "n": int(len(vals)),
                    "note": "n<20, insufficient", "t": None, "excess_pct": None,
                    "sign_matches": None}
        excess = float(vals.mean() - baseline.mean())
        t = IntradaySweepStudy._welch(vals, baseline)
        return {
            "hypothesis": name,
            "horizon": horizon,
            "n": int(len(vals)),
            "sweep_fwd_pct": round(float(vals.mean() * 100), 4),
            "baseline_pct": round(float(baseline.mean() * 100), 4),
            "excess_pct": round(excess * 100, 4),
            "t": round(float(t), 3),
            "sign_matches": bool(np.sign(excess) == expected_sign) if excess != 0 else None,
        }

    # --- full pre-registered battery -----------------------------------------------
    def battery(self, df):
        """The four hypotheses, evaluated at each horizon. Signs fixed in advance."""
        sweeps = df["is_sweep"]
        cells = [
            ("H1_vol_high", sweeps & df["vol_elevated"], +1, +1),
            ("H1_vol_low", sweeps & ~df["vol_elevated"], -1, +1),
            ("H2_ny", sweeps & df["is_ny"], +1, +1),
            ("H2_asia", sweeps & ~df["is_ny"], -1, +1),
            ("H3_deep", sweeps & df["deep_pierce"], +1, +1),
            ("H3_shallow", sweeps & ~df["deep_pierce"], -1, +1),
            ("H4_ny_highvol", sweeps & df["is_ny"] & df["vol_elevated"], +1, +1),
            ("H4_ny_lowvol", sweeps & df["is_ny"] & ~df["vol_elevated"], -1, +1),
        ]
        rows = []
        for name, mask, expected_sign, _ in cells:
            for h in HORIZONS:
                rows.append(self.evaluate_cell(df, name, mask, h, expected_sign))
        return pd.DataFrame(rows)

    # --- run with the out-of-sample split ------------------------------------------
    def run(self, level_col="level_rolling"):
        full = self.build(level_col=level_col)
        split = int(len(full) * DISCOVERY_FRACTION)
        disc, valid = full.iloc[:split].reset_index(drop=True), full.iloc[split:].reset_index(drop=True)

        print(f"🔬 [CONDITIONAL STUDY] {self.base.ticker} @ {self.base.interval} :: {level_col}")
        print(f"    total bars {len(full):,} | discovery {len(disc):,} | validation {len(valid):,}")
        print("    hypotheses pre-registered: H1 volume, H2 session, H3 depth, H4 combined")
        print(f"    volume threshold {VOLUME_ELEVATED_RATIO}x | NY hours {NY_START_HOUR}-{NY_END_HOUR} UTC"
              f" | depth split {DEPTH_SPLIT_BPS}bps\n")

        d = self.battery(disc)
        n_cells = int(d["t"].notna().sum())
        expected_fp = n_cells * 0.05
        print(f"--- DISCOVERY (first {DISCOVERY_FRACTION:.0%}) — exploratory, not evidence ---")
        print(f"    cells tested: {n_cells} | expected false positives at |t|>2 under the null: {expected_fp:.1f}")
        sig_d = d[(d["t"].notna()) & (d["t"].abs() >= 2.0) & (d["sign_matches"] == True)]
        print(f"    cells passing (|t|>=2 AND sign as pre-registered): {len(sig_d)}")
        if len(sig_d):
            print(sig_d[["hypothesis", "horizon", "n", "excess_pct", "t"]].to_string(index=False))
        print()

        v = self.battery(valid)
        n_cells_v = int(v["t"].notna().sum())
        expected_fp_v = n_cells_v * 0.05
        print(f"--- VALIDATION (last {1-DISCOVERY_FRACTION:.0%}) — the only evidence that counts ---")
        print(f"    cells tested: {n_cells_v} | expected false positives at |t|>2 under the null: {expected_fp_v:.1f}")
        sig_v = v[(v["t"].notna()) & (v["t"].abs() >= 2.0) & (v["sign_matches"] == True)]
        print(f"    cells passing (|t|>=2 AND sign as pre-registered): {len(sig_v)}")
        if len(sig_v):
            print(sig_v[["hypothesis", "horizon", "n", "excess_pct", "t"]].to_string(index=False))
        else:
            print("    (none)")
        print()

        # Confirmation: did discovery survivors hold up out-of-sample?
        surv = set(zip(sig_d["hypothesis"], sig_d["horizon"])) if len(sig_d) else set()
        if surv:
            print("--- SURVIVOR CHECK: discovery cells re-tested on validation data ---")
            v_indexed = v.set_index(["hypothesis", "horizon"])
            confirmed = 0
            for key in sorted(surv):
                if key in v_indexed.index:
                    r = v_indexed.loc[key]
                    ok = bool(pd.notna(r["t"]) and abs(r["t"]) >= 2.0 and r["sign_matches"] is True)
                    confirmed += ok
                    print(f"    {key[0]:<16} h={key[1]}  discovery t={d.set_index(['hypothesis','horizon']).loc[key,'t']:+.2f}"
                          f"  validation t={r['t'] if pd.notna(r['t']) else float('nan'):+.2f}  {'CONFIRMED' if ok else 'not confirmed'}")
            print(f"    confirmed out-of-sample: {confirmed}/{len(surv)}")
        else:
            print("--- SURVIVOR CHECK: nothing passed discovery, so nothing to confirm ---")

        print()
        return {"discovery": d, "validation": v, "expected_fp_validation": expected_fp_v}


def main():
    study = ConditionalMicrostructureStudy()
    try:
        for level in ("level_rolling", "level_prior_day"):
            study.run(level_col=level)
    except IntradayDataUnavailable as exc:
        print(f"🔴 [CONDITIONAL STUDY ABORTED] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
