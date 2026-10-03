"""
SYSTEM MODULE: ROBUSTNESS OF THE SWEEP FINDINGS
ROLE: TEST WHETHER THE SESSION AND FORECAST RESULTS SURVIVE THEIR OBVIOUS CONTROLS

Two of this project's findings needed a control that had not been applied. This module
applies both, because in each case the finding would be an artefact if the control
explains it.

R1 — SESSION CLUSTERING (premise falsified)
    The claim was that sweeps cluster in the New York session. But if NY simply moves
    more, MORE bars cross a rolling low there purely because more ground is covered.
    Control: normalise the pierce rate by the session's mean absolute return, giving
    "pierces per unit of movement". If that ratio is ~1.0, clustering is an artefact of
    volatility, not a session effect.

R2 — FORECAST INCREMENTALITY (the key forecast control)
    The claim is that sweep state forecasts volatility. But a sweep happens in conditions
    that are ALREADY volatile, so the sweep may only be proxying today's volatility.
    Control: include the current bar's absolute return. If the sweep coefficient dies
    once current volatility is present, the "forecast" is just volatility persistence.

Both controls are the stricter null. A finding that cannot survive them is not a finding.
"""

import os
import sys

import numpy as np
import pandas as pd

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from microstructure_crossasset import CrossAssetStudy, ASSETS, ReplicationDataUnavailable  # noqa: E402
from volatility_forecast import _log_rv, _ols, _ewma_vol, _dm_test, TRAIN_FRACTION  # noqa: E402

NY_HOURS = (13, 19)   # 13:00-18:59 UTC


class RobustnessStudy:
    def __init__(self, base=None):
        self.base = base or CrossAssetStudy()

    # --- R1: session clustering vs volatility --------------------------------------
    def session_control(self, symbol):
        df = self.base.build(self.base.load(symbol))
        df["hour"] = df["timestamp"].dt.hour
        df["ny"] = df["hour"].between(NY_HOURS[0], NY_HOURS[1] - 1)
        df["pierce"] = df["low"] < df["level"]
        df["absret"] = df["close"].pct_change().abs()
        df = df.dropna(subset=["absret"])
        g = df.groupby("ny").agg(pierce_rate=("pierce", "mean"),
                                 mean_absret=("absret", "mean"))
        if True not in g.index or False not in g.index:
            return None
        raw = float(g.loc[True, "pierce_rate"] / g.loc[False, "pierce_rate"])
        # Pierces per unit of movement: the volatility-neutral comparison.
        per_unit = g["pierce_rate"] / g["mean_absret"]
        norm = float(per_unit.loc[True] / per_unit.loc[False])
        return {
            "asset": ASSETS.get(symbol, symbol),
            "ny_pierce_rate": round(float(g.loc[True, "pierce_rate"]), 4),
            "other_pierce_rate": round(float(g.loc[False, "pierce_rate"]), 4),
            "ny_mean_absret": round(float(g.loc[True, "mean_absret"]), 6),
            "other_mean_absret": round(float(g.loc[False, "mean_absret"]), 6),
            "raw_ratio": round(raw, 3),
            "vol_normalised_ratio": round(norm, 3),
            "survives": bool(norm > 1.15),   # needs to be meaningfully above 1 to be real
        }

    # --- R2: forecast incrementality vs current volatility -------------------------
    def forecast_control(self, symbol, horizon=4):
        df = self.base.build(self.base.load(symbol))
        ret = df["close"].pct_change()
        out = df.copy()
        out["ret"] = ret
        out["ewma_logvol"] = _ewma_vol(ret.to_numpy())
        out["cur_absret"] = ret.abs()
        out = out.dropna(subset=["ewma_logvol", "cur_absret"]).reset_index(drop=True)

        y = _log_rv(out["ret"].to_numpy(), horizon)
        frame = out.assign(_y=y).dropna(subset=["_y"]).reset_index(drop=True)
        if len(frame) < 300:
            return None
        tr = frame.iloc[:int(len(frame) * TRAIN_FRACTION)]
        te = frame.iloc[int(len(frame) * TRAIN_FRACTION):]

        def design(f, cols):
            return np.column_stack([np.ones(len(f))] + [f[c].to_numpy().astype(float) for c in cols])

        rows = {}
        for label, cols in (("ewma", ["ewma_logvol"]),
                            ("ewma+curvol", ["ewma_logvol", "cur_absret"]),
                            ("ewma+curvol+sweep", ["ewma_logvol", "cur_absret", "is_sweep"])):
            Xtr, Xte = design(tr, cols), design(te, cols)
            coef, _, tvals = _ols(Xtr, tr["_y"].to_numpy())
            if coef is None:
                continue
            yte = te["_y"].to_numpy()
            pred = Xte @ coef
            rmse = float(np.sqrt(np.mean((yte - pred) ** 2)))
            rows[label] = {
                "cols": cols,
                "rmse": rmse,
                "sweep_t": round(float(tvals[-1]), 3) if cols[-1] == "is_sweep" else None,
                "pred_te": pred,
            }

        if "ewma" not in rows or "ewma+curvol+sweep" not in rows:
            return None
        # Does adding sweep to EWMA+curvol improve the forecast out of sample?
        dm = _dm_test(te["_y"].to_numpy() - rows["ewma+curvol"]["pred_te"],
                      te["_y"].to_numpy() - rows["ewma+curvol+sweep"]["pred_te"], h=horizon)
        return {
            "asset": ASSETS.get(symbol, symbol),
            "horizon": horizon,
            "rmse_ewma": round(rows["ewma"]["rmse"], 5),
            "rmse_ewma_curvol": round(rows["ewma+curvol"]["rmse"], 5),
            "rmse_ewma_curvol_sweep": round(rows["ewma+curvol+sweep"]["rmse"], 5),
            "sweep_t_with_curvol": rows["ewma+curvol+sweep"]["sweep_t"],
            "dm_t_vs_curvol": round(dm, 3) if dm == dm else None,
            # Survives only if the sweep still adds, with current vol controlled.
            "survives": bool(dm == dm and dm >= 2.0),
        }

    def run(self):
        print("🔬 [ROBUSTNESS] do the session and forecast findings survive their controls?\n")

        print("=== R1: SESSION CLUSTERING -- real, or just 'NY moves more'? ===")
        rows = []
        for sym in ASSETS:
            try:
                r = self.session_control(sym)
                if r:
                    rows.append(r)
            except ReplicationDataUnavailable:
                pass
        if rows:
            sdf = pd.DataFrame(rows)
            print(sdf[["asset", "raw_ratio", "vol_normalised_ratio", "survives"]].to_string(index=False))
            print()
            print(f"    raw ratio median:            {float(sdf['raw_ratio'].median()):.3f}x")
            print(f"    volatility-normalised median: {float(sdf['vol_normalised_ratio'].median()):.3f}x")
            print(f"    assets where it survives:     {int(sdf['survives'].sum())}/{len(sdf)}")
            if int(sdf["survives"].sum()) <= len(sdf) // 2:
                print("    ❌ FALSIFIED: session clustering is an artefact of volatility.")
                print("       NY pierces levels more often only because it moves more.")
            else:
                print("    ✅ survives the volatility normalisation")
        print()

        print("=== R2: FORECAST -- does sweep add anything beyond CURRENT volatility? ===")
        rows = []
        for sym in ASSETS:
            try:
                r = self.forecast_control(sym)
                if r:
                    rows.append(r)
            except ReplicationDataUnavailable:
                pass
        if rows:
            fdf = pd.DataFrame(rows)
            print(fdf[["asset", "horizon", "rmse_ewma", "rmse_ewma_curvol",
                       "rmse_ewma_curvol_sweep", "sweep_t_with_curvol",
                       "dm_t_vs_curvol", "survives"]].to_string(index=False))
            print()
            print(f"    assets where sweep still adds (DM>=2 vs EWMA+curvol): "
                  f"{int(fdf['survives'].sum())}/{len(fdf)}")
            med_imp = float(((fdf['rmse_ewma_curvol'] - fdf['rmse_ewma_curvol_sweep'])
                             / fdf['rmse_ewma_curvol'] * 100).median())
            print(f"    median RMSE improvement over EWMA+curvol: {med_imp:+.4f}%")
            if int(fdf["survives"].sum()) >= len(fdf) * 0.5:
                print("    ✅ sweep state adds information beyond current volatility")
            else:
                print("    ❌ DOES NOT SURVIVE: with current volatility controlled, sweep state")
                print("       adds little. The 'forecast' is largely volatility persistence.")
        return {"session": rows, "forecast": rows}


def main():
    study = RobustnessStudy()
    try:
        study.run()
    except ReplicationDataUnavailable as exc:
        print(f"🔴 [ROBUSTNESS ABORTED] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
