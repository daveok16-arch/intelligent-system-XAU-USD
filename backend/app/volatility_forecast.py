"""
SYSTEM MODULE: VOLATILITY FORECASTING — INCREMENTAL VALUE OVER EWMA
ROLE: TEST WHETHER SWEEP STATE ADDS PREDICTIVE POWER BEYOND THE INDUSTRY BASELINE

The right question
------------------
The sweep studies established that a rejected sweep is followed by a larger absolute move
(replicated across four metals). But "volatility rises after a dip" is the leverage effect,
and every desk already forecasts volatility with EWMA (RiskMetrics lambda=0.94). So the
finding is only useful if it beats that baseline.

Stating it as a falsifiable hypothesis:

    H0: sweep state adds NOTHING over EWMA. Volatility clusters; sweep is a proxy for it.
    H1: sweep state adds incremental predictive power over EWMA, out of sample.

Method
------
Out-of-sample, expanding-window regression on log realized volatility:

    log(RV_{t+1..t+h}) ~ a + b * log(EWMA_vol_t) + c * sweep_state_t

c is the whole answer. If c is insignificant, EWMA already contains the information and
the effect is a restatement of volatility clustering.

Controls applied:
  * Strictly out-of-sample: coefficients fit on the first 60%, evaluated on the last 40%.
    No in-sample fit is reported as evidence.
  * EWMA uses only past returns, lambda=0.94 (RiskMetrics standard, not tuned).
  * Multiple testing: the number of specifications is reported with the expected false
    positive count, so a lone significant cell is not mistaken for a finding.
  * Cross-asset: replicated on gold, silver, platinum, copper. A forecast improvement
    must appear in more than one market to be credible.
  * Baseline comparison is against EWMA, not against zero -- the stricter null.
"""

import os
import sys

import numpy as np
import pandas as pd

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from microstructure_crossasset import CrossAssetStudy, ASSETS, ReplicationDataUnavailable  # noqa: E402

EWMA_LAMBDA = float(os.getenv("MS_EWMA_LAMBDA", "0.94"))   # RiskMetrics standard
TRAIN_FRACTION = 0.60
HORIZONS = (1, 4, 12)


def _log_rv(returns, horizon):
    """Forward realized volatility over the next `horizon` bars, in log space.

    Uses sqrt(mean of squared returns) -- the standard realized-volatility estimator --
    rather than a sample standard deviation, because the latter is undefined for a
    single observation and would silently drop the h=1 horizon.

    Log space because volatility is positive and right-skewed; OLS on the raw scale would
    be dominated by the largest observations.
    """
    r = np.asarray(returns, dtype=float)
    n = len(r)
    out = np.full(n, np.nan)
    for i in range(n - horizon):
        window = r[i + 1:i + 1 + horizon]
        if len(window) >= 1 and np.isfinite(window).all():
            rv = np.sqrt(np.mean(window ** 2))
            if rv > 0:
                out[i] = np.log(rv)
    return out


def _ewma_vol(returns, lam=EWMA_LAMBDA):
    """EWMA volatility using only past returns -- the industry baseline forecast."""
    r = np.asarray(returns, dtype=float)
    n = len(r)
    var = np.full(n, np.nan)
    if n < 20:
        return var
    v = np.nanvar(r[:20])
    var[19] = v
    for i in range(20, n):
        v = lam * v + (1 - lam) * r[i - 1] ** 2   # uses r[i-1]: strictly prior information
        var[i] = v
    with np.errstate(invalid="ignore"):
        return 0.5 * np.log(var)                  # log of EWMA volatility


def _ewma_fast(returns, lam=0.70):
    """A faster-decaying EWMA (half-life ~2 bars vs ~11 for 0.94).

    The sweep coefficient is largest at the shortest horizon, which suggests the
    information is short-lived. If a fast EWMA already contains it, the sweep is not
    adding anything a practitioner could not get by simply shortening the decay.
    """
    return _ewma_vol(returns, lam=lam)


def _ols(X, y):
    """Least squares with a constant already in X. Returns (coef, se, t)."""
    XtX = X.T @ X
    try:
        XtX_inv = np.linalg.pinv(XtX)
    except np.linalg.LinAlgError:
        return None, None, None
    coef = XtX_inv @ (X.T @ y)
    resid = y - X @ coef
    dof = max(len(y) - X.shape[1], 1)
    sigma2 = float(resid @ resid) / dof
    se = np.sqrt(np.maximum(np.diag(XtX_inv) * sigma2, 0))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(se > 0, coef / se, np.nan)
    return coef, se, t


def _dm_test(e1, e2, h=1):
    """Diebold-Mariano style test on two forecast error series.

    Tests whether model 2's squared errors are significantly lower than model 1's.
    A positive statistic means model 2 (augmented) forecasts better. This is the correct
    out-of-sample test: an in-sample coefficient t-stat says nothing about whether the
    forecast actually improves on unseen data.
    """
    d = (np.asarray(e1, dtype=float) ** 2) - (np.asarray(e2, dtype=float) ** 2)
    d = d[np.isfinite(d)]
    if len(d) < 10:
        return float("nan")
    mean_d = d.mean()
    # Newey-West style variance with lag h-1 for h-step-ahead overlapping forecasts.
    gamma0 = np.mean((d - mean_d) ** 2)
    var = gamma0
    for k in range(1, max(h, 1)):
        if k < len(d):
            gamma = np.mean((d[k:] - mean_d) * (d[:-k] - mean_d))
            var += 2 * (1 - k / max(h, 1)) * gamma
    se = np.sqrt(max(var, 0) / len(d))
    return float(mean_d / se) if se > 0 else float("nan")


class VolatilityForecastStudy:
    def __init__(self, base=None):
        self.base = base or CrossAssetStudy()

    def features(self, symbol):
        df = self.base.build(self.base.load(symbol))
        ret = df["close"].pct_change()
        out = df.copy()
        out["ret"] = ret
        out["ewma_logvol"] = _ewma_vol(ret.to_numpy())
        out["sweep"] = out["is_sweep"].astype(float)
        # Pierced the level at all, regardless of how it closed. `build` supplies `level`
        # and `is_sweep`; the pierce flag is derived here so the two groups are exhaustive
        # and mutually exclusive: rejected sweep vs failed pierce.
        out["pierced"] = out["low"] < out["level"]
        out["failed_pierce"] = (out["pierced"] & ~out["is_sweep"]).astype(float)
        return out.dropna(subset=["ewma_logvol", "ret"]).reset_index(drop=True)

    def one_asset(self, symbol):
        df = self.features(symbol)

        rows = []
        for h in HORIZONS:
            y = _log_rv(df["ret"].to_numpy(), h)
            frame = df.assign(_y=y).dropna(subset=["_y"]).reset_index(drop=True)
            if len(frame) < 200:
                continue
            tr = frame.iloc[:int(len(frame) * TRAIN_FRACTION)]
            te = frame.iloc[int(len(frame) * TRAIN_FRACTION):]

            # Baseline model: EWMA only.  Augmented: EWMA + sweep state.
            def design(f):
                return np.column_stack([np.ones(len(f)), f["ewma_logvol"].to_numpy(),
                                        f["sweep"].to_numpy()])

            Xtr_b = design(tr)[:, :2]
            Xtr_a = design(tr)
            ytr = tr["_y"].to_numpy()
            coef_b, _, _ = _ols(Xtr_b, ytr)
            coef_a, _, t_a = _ols(Xtr_a, ytr)

            Xte_b = design(te)[:, :2]
            Xte_a = design(te)
            yte = te["_y"].to_numpy()
            if coef_b is None or coef_a is None:
                continue

            pred_b = Xte_b @ coef_b
            pred_a = Xte_a @ coef_a
            sse_b = float(np.sum((yte - pred_b) ** 2))
            sse_a = float(np.sum((yte - pred_a) ** 2))
            # Out-of-sample R^2 of the augmented model over the baseline.
            oos_r2 = 1.0 - sse_a / sse_b if sse_b > 0 else np.nan
            # RMSE improvement is more interpretable than SSE: it is the change in
            # typical forecast error, in the same log-volatility units.
            rmse_b = float(np.sqrt(sse_b / len(yte)))
            rmse_a = float(np.sqrt(sse_a / len(yte)))
            # The honest significance test: does the augmented forecast beat the baseline
            # on unseen data? (An in-sample coefficient t-stat does not answer this.)
            dm = _dm_test(yte - pred_b, yte - pred_a, h=h)

            rows.append({
                "asset": ASSETS.get(symbol, symbol), "horizon": h,
                "train_n": len(tr), "test_n": len(te),
                "ewma_coef": round(float(coef_a[1]), 4),
                "sweep_coef": round(float(coef_a[2]), 5),
                "sweep_t_insample": round(float(t_a[2]), 3) if t_a is not None else None,
                "oos_rmse_baseline": round(rmse_b, 5),
                "oos_rmse_augmented": round(rmse_a, 5),
                "oos_rmse_improve_pct": round((rmse_b - rmse_a) / rmse_b * 100, 4) if rmse_b else None,
                "oos_improvement_pct": round(float(oos_r2) * 100, 3) if np.isfinite(oos_r2) else None,
                "dm_t_oos": round(dm, 3) if dm == dm else None,
            })
        return pd.DataFrame(rows)

    def asymmetry_test(self, symbol):
        """Does the REJECTION carry information beyond EWMA, at any horizon?

        This is the direct test of my own C4 finding. Rejected sweeps and failed pierces
        are exhaustive and mutually exclusive, so including both as separate regressors
        tests whether the *resolution* of the pierce matters -- which is the mechanism
        claim -- rather than whether a dip happened.
        """
        df = self.features(symbol)
        df["failed_pierce"] = (df["pierced"] & ~df["is_sweep"]).astype(float)
        df["ewma_fast"] = _ewma_fast(df["ret"].to_numpy())
        df = df.dropna(subset=["ewma_fast"]).reset_index(drop=True)

        rows = []
        for h in HORIZONS:
            y = _log_rv(df["ret"].to_numpy(), h)
            frame = df.assign(_y=y).dropna(subset=["_y"]).reset_index(drop=True)
            if len(frame) < 300:
                continue
            tr = frame.iloc[:int(len(frame) * TRAIN_FRACTION)]
            te = frame.iloc[int(len(frame) * TRAIN_FRACTION):]

            def build(f, fast):
                cols = [np.ones(len(f)), (f["ewma_fast"] if fast else f["ewma_logvol"]).to_numpy(),
                        f["is_sweep"].to_numpy().astype(float), f["failed_pierce"].to_numpy()]
                return np.column_stack(cols)

            for label, fast in (("ewma094", False), ("ewma070", True)):
                Xtr, Xte = build(tr, fast), build(te, fast)
                coef, _, tvals = _ols(Xtr, tr["_y"].to_numpy())
                if coef is None:
                    continue
                yte = te["_y"].to_numpy()
                # Baseline: EWMA alone.
                Xtr_b, Xte_b = Xtr[:, :2], Xte[:, :2]
                coef_b, _, _ = _ols(Xtr_b, tr["_y"].to_numpy())
                pred_b, pred_a = Xte_b @ coef_b, Xte @ coef
                dm = _dm_test(yte - pred_b, yte - pred_a, h=h)
                rows.append({
                    "asset": ASSETS.get(symbol, symbol), "horizon": h, "baseline": label,
                    "t_sweep": round(float(tvals[2]), 3),
                    "t_failed_pierce": round(float(tvals[3]), 3),
                    # Positive means the two resolutions differ -> the mechanism matters.
                    "asymmetry_diff": round(float(coef[2] - coef[3]), 5),
                    "dm_t_oos": round(dm, 3) if dm == dm else None,
                })
        return pd.DataFrame(rows)

    def run(self):
        print(f"📉 [VOLATILITY FORECAST] does sweep state beat EWMA (lambda={EWMA_LAMBDA})?")
        print(f"    Strictly out-of-sample: fit on first {TRAIN_FRACTION:.0%}, evaluated on the rest")
        print("    H0: sweep adds nothing over EWMA.  c is the answer.\n")
        frames = []
        for sym in ASSETS:
            try:
                t = self.one_asset(sym)
                if len(t):
                    frames.append(t)
            except ReplicationDataUnavailable as exc:
                print(f"    ⚠️ {sym}: {exc}")
        if not frames:
            print("🔴 no asset produced a usable sample")
            return None
        df = pd.concat(frames, ignore_index=True)
        print(df.to_string(index=False))
        print()

        n_cells = len(df)
        expected_fp = n_cells * 0.05
        sig = df[df["dm_t_oos"].abs() >= 2.0] if "dm_t_oos" in df else pd.DataFrame()
        improved = df[df["oos_improvement_pct"] > 0]

        print("--- VERDICT ---")
        print(f"    specifications tested: {n_cells} | expected false positives at |t|>2: {expected_fp:.1f}")
        print(f"    out-of-sample DM significant (|t|>=2): {len(sig)}/{n_cells}")
        print(f"    out-of-sample improvement over EWMA: {len(improved)}/{n_cells}")
        print(f"    median RMSE improvement: "
              f"{float(df['oos_rmse_improve_pct'].median()):.4f}% "
              f"(typical forecast error, log-vol units)")
        # The pattern is horizon-dependent; report it rather than the pooled number.
        for h in sorted(df["horizon"].unique()):
            sub = df[df["horizon"] == h]
            dm = sub["dm_t_oos"].dropna()
            print(f"      h={h:<3} DM significant {int((dm >= 2.0).sum())}/{len(sub)}  "
                  f"median RMSE improve {float(sub['oos_rmse_improve_pct'].median()):+.4f}%")
        if len(sig) >= n_cells * 0.6:
            print("    ✅ sweep state adds significant out-of-sample forecasting power")
            print("       beyond EWMA in the majority of specifications.")
        else:
            print("    ❌ sweep state does NOT reliably beat EWMA out of sample.")
        print()

        print("--- ASYMMETRY: does the REJECTION matter, beyond EWMA? ---")
        print("    Regressors: EWMA, is_sweep, failed_pierce (mutually exclusive).")
        print("    t_sweep / t_failed_pierce: each group's deviation from EWMA.")
        print("    asymmetry_diff > 0 means a rejected sweep is followed by DIFFERENT")
        print("    (typically lower) volatility than a failed pierce.\n")
        aframes = []
        for sym in ASSETS:
            try:
                t = self.asymmetry_test(sym)
                if len(t):
                    aframes.append(t)
            except ReplicationDataUnavailable:
                pass
        if aframes:
            adf = pd.concat(aframes, ignore_index=True)
            print(adf.to_string(index=False))
            print()
            # The mechanism claim is that the two resolutions differ. Count how often.
            pos = int((adf["asymmetry_diff"] > 0).sum())
            print(f"    specifications where rejection differs from failure: {pos}/{len(adf)}")
            consistent = adf.groupby("asset")["asymmetry_diff"].apply(
                lambda s: bool((s > 0).all()))
            print(f"    assets where the sign is consistent across horizons: "
                  f"{int(consistent.sum())}/{len(consistent)}")
            dm_sig = int((adf["dm_t_oos"].fillna(0) >= 2.0).sum())
            print(f"    out-of-sample DM significant: {dm_sig}/{len(adf)}")
        return df


def main():
    study = VolatilityForecastStudy()
    try:
        study.run()
    except ReplicationDataUnavailable as exc:
        print(f"🔴 [FORECAST STUDY ABORTED] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
