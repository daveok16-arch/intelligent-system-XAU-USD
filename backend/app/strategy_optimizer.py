"""
SYSTEM MODULE: UNIFIED STRATEGY OPTIMIZATION MATRIX
ROLE: SWEEP THE PARAMETER TENSOR FOR THE MACRO x SPATIAL INTERACTION

Tests whether the Sweep_Floor entry carries edge, (a) alone and (b) only when the macro
regime approves, across a grid of target/stop multiples and holding windows -- always
measured against the unconditional hold over the same horizon.

Corrections vs the Directive 15 draft. The draft was the most dangerous artefact in this
project because it produced spectacular-looking numbers that were pure algebra:

  1. `gold_historical_daily_raw.csv` does not exist, so the draft took its fallback branch
     and SYNTHESISED prices from the structural columns:
         Close = Sweep_Floor + 2.0*ATR ;  Low = Sweep_Floor - 0.5*ATR ;  High = Close + ATR
     It then backtested a sweep strategy against those invented prices. That is circular:
     the "market" is derived from the very indicator being traded.
  2. The construction made the strategy UNLOSABLE. With High = floor + 3*ATR and
     Low = floor - 0.5*ATR, a 2.5*ATR target is always reached and a 2.0*ATR stop is never
     hit. The draft reported t-stats of 634, 388, 301 -- not edge, arithmetic.
  3. It inspected only the entry bar, so holding windows did not exist.
  4. Its "baseline" was (Close - entry)/entry on the same synthetic bar, i.e. the same
     fabricated series, so the comparison was self-referential.
  5. Its test only asserted the return value is not None.

This engine uses real GC=F OHLC and REUSES the already-verified SpatialEdgeBacktester
(look-ahead-free floor, multi-bar walk-forward, real costs) so the two backtests cannot
diverge in methodology.
"""

import os
import sys
import itertools

import numpy as np
import pandas as pd

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from backtest_spatial import SpatialEdgeBacktester, SpatialBacktestDataUnavailable  # noqa: E402

_BACKEND_DIR = os.path.dirname(_APP_DIR)
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)
_DATA_DIR = os.getenv("DATA_DIR", os.path.join(_PROJECT_DIR, "data"))

TARGET_MULTS = [float(x) for x in os.getenv("OPT_TARGET_MULTS", "0.5,1.0,1.5,2.0,2.5,3.0").split(",")]
STOP_MULTS = [float(x) for x in os.getenv("OPT_STOP_MULTS", "0.5,1.0,1.5,2.0,2.5").split(",")]
HOLD_WINDOWS = [int(x) for x in os.getenv("OPT_HOLD_WINDOWS", "1,2,3,5,10").split(",")]
MIN_TRADES = int(os.getenv("OPT_MIN_TRADES", "30"))


class IntegratedStrategyOptimizer:
    def __init__(self, data_dir=None, ticker="GC=F"):
        self.data_dir = data_dir or _DATA_DIR
        self.engine = SpatialEdgeBacktester(ticker=ticker, data_dir=self.data_dir)

    # --- macro regime attached to daily bars -------------------------------------
    def load_bars_with_regime(self):
        """Real daily bars + the macro gate state prevailing at each bar (backward
        as-of, so no future macro reading is visible to a past bar)."""
        bars = self.engine.build_structure(self.engine.load_daily_bars())

        macro_path = os.getenv("MACRO_REPO_CSV", os.path.join(self.data_dir, "macro_intelligence_repository.csv"))
        if not os.path.exists(macro_path) or os.path.getsize(macro_path) == 0:
            # No macro history on disk. Do NOT fake a gate; mark it unknown and let the
            # caller fall back to the ungated test rather than inventing "OPEN".
            bars["MACRO_GATE"] = None
            bars["SDI"] = np.nan
            return bars

        macro = pd.read_csv(macro_path)
        macro = macro.rename(columns={"week_ending_date": "gate_date"})
        macro["gate_date"] = pd.to_datetime(macro["gate_date"], errors="coerce")
        macro = macro.dropna(subset=["gate_date"]).sort_values("gate_date")
        macro["bars_date"] = macro["gate_date"].astype("datetime64[ns]")

        bars = bars.assign(bars_date=bars["Date"].astype("datetime64[ns]")).sort_values("bars_date")
        merged = pd.merge_asof(bars, macro[["bars_date", "MACRO_GATE", "SDI"]],
                               on="bars_date", direction="backward")
        return merged

    # --- one grid cell -------------------------------------------------------------
    def evaluate_cell(self, bars, target_mult, stop_mult, hold_bars, use_macro, engine=None):
        engine = engine or self.engine
        trades = engine.simulate(bars, atr_target_mult=target_mult, atr_stop_mult=stop_mult,
                                 hold_bars=hold_bars)
        if trades.empty:
            return None

        if use_macro:
            if "MACRO_GATE" not in bars.columns:
                return None
            # Only trades taken when the prevailing gate was OPEN count as in-regime.
            allowed = bars.set_index("Date")["MACRO_GATE"]
            trades = trades[trades["Date"].map(allowed).eq("OPEN")]
            if len(trades) < MIN_TRADES:
                return None

        rets = trades["Return"].to_numpy()
        if len(rets) < MIN_TRADES:
            return None

        # Baseline: unconditional forward hold over the SAME horizon, all bars.
        baseline = engine.unconditional_forward_returns(bars, horizon=hold_bars)

        excess = rets.mean() - baseline.mean()
        t_stat = self._welch(rets, baseline)
        wins = rets > 0
        gross_loss = -rets[~wins].sum()
        return {
            "Macro_Gated": use_macro,
            "Target_Mult": target_mult,
            "Stop_Mult": stop_mult,
            "Hold_Bars": hold_bars,
            "Trade_Count": int(len(rets)),
            "Win_Rate_Pct": round(float(wins.mean() * 100), 2),
            "Avg_Strategy_Return_Pct": round(float(rets.mean() * 100), 4),
            "Avg_Baseline_Return_Pct": round(float(baseline.mean() * 100), 4),
            "Excess_Alpha_Pct": round(float(excess * 100), 4),
            "Profit_Factor": round(float(rets[wins].sum() / gross_loss), 4) if gross_loss > 0 else "inf",
            "True_T_Stat_vs_Baseline": round(float(t_stat), 4) if t_stat == t_stat else None,
            "Notes": "in-regime subset" if use_macro else "all sweeps",
        }

    @staticmethod
    def _welch(a, b):
        if len(a) < 2 or len(b) < 2:
            return float("nan")
        se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
        return float((a.mean() - b.mean()) / se) if se > 0 else float("nan")

    # --- full tensor ---------------------------------------------------------------
    def run_combinatorial_sweep(self):
        bars = self.load_bars_with_regime()
        has_regime = "MACRO_GATE" in bars.columns and bars["MACRO_GATE"].notna().any()

        results = []
        for target_mult, stop_mult, hold_bars, use_macro in itertools.product(
            TARGET_MULTS, STOP_MULTS, HOLD_WINDOWS, [False, True]
        ):
            if use_macro and not has_regime:
                continue
            row = self.evaluate_cell(bars, target_mult, stop_mult, hold_bars, use_macro)
            if row:
                results.append(row)

        df = pd.DataFrame(results)
        df.attrs["macro_history_available"] = bool(has_regime)
        return df


def main():
    print("📋 --- UNIFIED OPTIMIZATION MATRIX (real GC=F OHLC; vs unconditional baseline) ---")
    opt = IntegratedStrategyOptimizer()
    try:
        grid = opt.run_combinatorial_sweep()
    except SpatialBacktestDataUnavailable as exc:
        print(f"🔴 [OPTIMIZER ABORTED] {exc}")
        return 1

    if not opt.load_bars_with_regime()["MACRO_GATE"].notna().any():
        print("⚠️  No macro history on disk: the macro-gated half of the tensor cannot be "
              "evaluated honestly and was skipped (rather than faking a gate).")

    if grid.empty:
        print("No grid cell produced a sufficient sample.")
        return 0

    top = grid.sort_values("True_T_Stat_vs_Baseline", ascending=False).head(10)
    print(top.to_string(index=False))

    # The decisive check, independent of any exit convention.
    print("\n--- CONVENTION-FREE SWEEP PREDICTIVENESS (buy-at-close both sides) ---")
    bars = opt.load_bars_with_regime()
    conv = opt.engine.sweep_predictive_test(bars)
    print(conv.to_string(index=False))

    best = grid["True_T_Stat_vs_Baseline"].max()
    n_cells = len(grid)
    print(f"\nCell(s) evaluated: {n_cells}")
    print(f"Best t vs baseline: {best}")
    passed = grid[grid["True_T_Stat_vs_Baseline"].abs() > 2.0]
    if passed.empty:
        print("\n🏁 THE HYPOTHESIS IS CLOSED: no coordinate in the tensor reaches |t| > 2 "
              "against the baseline.")
    else:
        print(f"\n⚠️  {len(passed)} of {n_cells} cells exceed |t| > 2 in-sample. With a true "
              f"null only ~5% should, so this reflects a systematic bias (different entry "
              f"conventions between strategy and baseline), not edge. See the "
              f"convention-free table above for the unbiased answer.")
    if (conv["Edge_Pct"] <= 0).all():
        print("\n🏁 SWEEP EVENT PREDICTS NOTHING: forward returns after a sweep are no "
              "better than average at every horizon. There is no post-sweep bounce. "
              "The spatial mean-reversion hypothesis is closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
