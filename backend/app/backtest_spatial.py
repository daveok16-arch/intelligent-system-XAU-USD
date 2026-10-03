"""
SYSTEM MODULE: SPATIAL LIQUIDITY SWEEP BACKTEST ENGINE
ROLE: EVALUATE EXPECTANCY OF LIMIT ENTRIES AT THE SWEEP_FLOOR (STOP-HUNT ZONE)

Hypothesis under test: institutions sweep retail stops below the 3-day swing low, so a
limit buy placed inside that zone (Sweep_Floor = 3-day low - $1.50) catches a
mean-reverting bounce with positive expectancy.

Corrections vs the Directive 14 draft, each proven by running it:
  1. The draft read Low/High/Close from spatial_boundaries_repository.csv, which holds
     only structural columns (Date, Three_Day_High, Three_Day_Low, Sweep_Floor, ATR_14,
     Source). It died with `KeyError: ['Low', 'High']`. Real daily OHLC is fetched here.
  2. The draft only ever inspected the ENTRY bar, so the stated "3-day holding period"
     was never implemented -- if neither target nor stop hit intraday it exited at that
     same bar's close (a hold of ~0 days). This walks forward properly.
  3. THE MOST IMPORTANT ONE: the draft tested average trade return against ZERO. Gold
     has a strong secular upward drift, so ANY long-only rule shows a "significant"
     positive mean for free. The correct null is the UNCONDITIONAL forward return over
     the same horizon. Without that baseline, a bull market masquerades as edge.
  4. When a single bar spans both the stop and the target, OHLC alone cannot say which
     was touched first. The draft silently assumed stop-first (pessimistic). This
     reports how often that ambiguity arises so the reader can weigh it.
  5. Gap-through fill: a limit at the floor fills at the OPEN when the bar opens below
     it, not at the floor. The draft assumed a fill at the floor regardless.

Fabricates nothing: raises if real OHLC is unavailable.
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

_DATA_DIR = os.getenv("DATA_DIR", os.path.join(_PROJECT_DIR, "data"))

SWEEP_BUFFER_USD = 1.50          # 150 pips at $0.01/pip
SWING_WINDOW = 3
ATR_WINDOW = 14
HOLD_BARS = int(os.getenv("SPATIAL_HOLD_BARS", "3"))
COST_BPS = float(os.getenv("SPATIAL_COST_BPS", "5.0"))


class SpatialBacktestDataUnavailable(RuntimeError):
    """Real OHLC could not be obtained; a spatial test must not be simulated."""


class SpatialEdgeBacktester:
    def __init__(self, ticker="GC=F", start="2000-08-30", data_dir=None):
        self.ticker = ticker
        self.start = start
        self.data_dir = data_dir or _DATA_DIR

    # --- real OHLC ---------------------------------------------------------------
    def load_daily_bars(self):
        df = yf.download(self.ticker, start=self.start, interval="1d",
                         progress=False, auto_adjust=False)
        if df is None or df.empty:
            raise SpatialBacktestDataUnavailable(f"no OHLC returned for {self.ticker}")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.reset_index()
        date_col = "Date" if "Date" in df.columns else df.columns[0]
        df = df.rename(columns={date_col: "Date"})
        missing = [c for c in ("High", "Low", "Close") if c not in df.columns]
        if missing:
            raise SpatialBacktestDataUnavailable(f"OHLC payload missing {missing}")
        df = df[["Date", "High", "Low", "Close", "Open"]].dropna()
        if df.empty:
            raise SpatialBacktestDataUnavailable("OHLC contained no complete rows")
        return df.sort_values("Date").reset_index(drop=True)

    @staticmethod
    def build_structure(df):
        """Structural boundary using only information available BEFORE each bar."""
        out = df.copy()
        # Shift first: the floor usable for bar t is derived from bars t-3..t-1.
        out["Floor"] = out["Low"].shift(1).rolling(SWING_WINDOW).min() - SWEEP_BUFFER_USD
        high_low = out["High"] - out["Low"]
        high_prev = (out["High"] - out["Close"].shift(1)).abs()
        low_prev = (out["Low"] - out["Close"].shift(1)).abs()
        true_range = pd.concat([high_low, high_prev, low_prev], axis=1).max(axis=1)
        out["ATR"] = true_range.shift(1).rolling(ATR_WINDOW).mean()
        return out

    # --- simulation --------------------------------------------------------------
    def simulate(self, df, atr_target_mult=1.5, atr_stop_mult=1.0, gap_aware=True,
                 hold_bars=None, allow_entry_bar_exit=False):
        """Walk each sweep forward up to `hold_bars` (defaults to HOLD_BARS).

        `allow_entry_bar_exit=False` (default, and honest): the entry bar's High may have
        printed BEFORE its Low triggered the fill, so crediting a target hit on the entry
        bar assumes perfect intrabar foresight. Exits resolve from the NEXT bar forward.
        True reproduces that optimistic assumption so its impact can be measured.
        """
        hold_bars = hold_bars or HOLD_BARS
        trades = []
        n = len(df)
        lows = df["Low"].to_numpy()
        highs = df["High"].to_numpy()
        closes = df["Close"].to_numpy()
        opens = df["Open"].to_numpy()
        floors = df["Floor"].to_numpy()
        atrs = df["ATR"].to_numpy()
        dates = df["Date"].to_numpy()

        for i in range(n - hold_bars):
            floor, atr = floors[i], atrs[i]
            if np.isnan(floor) or np.isnan(atr) or atr <= 0:
                continue
            if lows[i] > floor:          # no sweep of the stop cluster
                continue

            # Limit buy at the floor. If the bar opened below it, the fill is at the
            # open (a better price) rather than the floor.
            entry = min(opens[i], floor) if gap_aware else floor

            stop_price = entry - atr_stop_mult * atr
            target_price = entry + atr_target_mult * atr

            exit_price, exit_reason, bars_held, both_touched = None, None, None, False

            # Entry bar: OHLC alone cannot say whether the High or the Low came first, so
            # by default we do NOT resolve exits on the bar we entered on.
            hit_stop = lows[i] <= stop_price
            hit_target = highs[i] >= target_price
            if allow_entry_bar_exit:
                if hit_stop and hit_target:
                    both_touched = True
                    exit_price, exit_reason, bars_held = stop_price, "stop_stop_first", 0
                elif hit_stop:
                    exit_price, exit_reason, bars_held = stop_price, "stop", 0
                elif hit_target:
                    exit_price, exit_reason, bars_held = target_price, "target", 0
            if exit_price is None:
                for j in range(i + 1, min(i + 1 + hold_bars, n)):
                    s = lows[j] <= stop_price
                    t = highs[j] >= target_price
                    if s and t:
                        both_touched = True
                        exit_price, exit_reason, bars_held = stop_price, "stop_stop_first", j - i
                        break
                    if s:
                        exit_price, exit_reason, bars_held = stop_price, "stop", j - i
                        break
                    if t:
                        exit_price, exit_reason, bars_held = target_price, "target", j - i
                        break
            if exit_price is None:       # neither hit within the horizon
                k = min(i + hold_bars, n - 1)
                exit_price, exit_reason, bars_held = closes[k], "time_exit", k - i

            gross = (exit_price - entry) / entry
            cost = COST_BPS / 10000.0
            trades.append({
                "Date": dates[i], "Entry": entry, "Exit": exit_price,
                "Reason": exit_reason, "Bars": bars_held,
                "Both_Touched": both_touched,
                "Return": gross - cost,
                "Gross_Return": gross,
            })
        return pd.DataFrame(trades)

    # --- baseline ----------------------------------------------------------------
    @staticmethod
    def unconditional_forward_returns(df, horizon=HOLD_BARS):
        """Forward return of simply holding for `horizon` bars from every bar. This is
        the correct null: gold drifts up, so a long-only rule beats zero for free."""
        close = df["Close"].to_numpy()
        out = []
        for i in range(len(df) - horizon):
            out.append((close[i + horizon] - close[i]) / close[i] - COST_BPS / 10000.0)
        return np.array(out)

    @staticmethod
    def _welch(a, b):
        if len(a) < 2 or len(b) < 2:
            return float("nan")
        se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
        return float((a.mean() - b.mean()) / se) if se > 0 else float("nan")

    # --- convention-free diagnostic --------------------------------------------------
    @staticmethod
    def sweep_predictive_test(bars, horizons=(1, 2, 3, 5)):
        """The authoritative test, free of entry-convention artefacts.

        Exit-pricing comparisons are contaminated: the strategy fills at the FLOOR while
        the baseline is measured from the bar CLOSE, and the floor sits ~0.2% below that
        close, handing the strategy a cheaper start by construction. This test uses ONE
        convention on both sides -- buy at the close -- and asks the only question that
        matters: is the forward return after a sweep different from the average?

        If the sweep event carries mean-reversion edge, sweep-bar forward returns should
        EXCEED the unconditional forward return. If they do not, the hypothesis is dead.
        """
        sweep = (bars["Low"] <= bars["Floor"]).to_numpy()
        out = []
        close = bars["Close"].to_numpy()
        for h in horizons:
            fwd = np.full(len(close), np.nan)
            fwd[:-h] = (close[h:] - close[:-h]) / close[:-h]
            sweep_fwd = fwd[sweep]
            all_fwd = fwd[~np.isnan(fwd)]
            sweep_fwd = sweep_fwd[~np.isnan(sweep_fwd)]
            out.append({
                "Horizon_Bars": h,
                "Sweep_Mean_Fwd_Pct": round(float(sweep_fwd.mean() * 100), 4) if len(sweep_fwd) else None,
                "Unconditional_Mean_Fwd_Pct": round(float(all_fwd.mean() * 100), 4),
                "Edge_Pct": round(float((sweep_fwd.mean() - all_fwd.mean()) * 100), 4) if len(sweep_fwd) else None,
                "T_Stat": round(float(SpatialEdgeBacktester._welch(sweep_fwd, all_fwd)), 4),
            })
        return pd.DataFrame(out)

    # --- report ------------------------------------------------------------------
    def run_spatial_evaluation(self, atr_target_mult=1.5, atr_stop_mult=1.0):
        bars = self.build_structure(self.load_daily_bars())
        trades = self.simulate(bars, atr_target_mult, atr_stop_mult)
        if trades.empty:
            return {"status": "NO_TRADES", "message": "Price never crossed a calculated sweep floor."}

        rets = trades["Return"].to_numpy()
        baseline = self.unconditional_forward_returns(bars)

        wins = rets > 0
        gross_profit = rets[wins].sum() if wins.any() else 0.0
        gross_loss = -rets[~wins].sum() if (~wins).any() else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        # vs zero (what the draft did): inflated by gold's drift.
        t_vs_zero = rets.mean() / (rets.std(ddof=1) / np.sqrt(len(rets))) if rets.std(ddof=1) > 0 else 0.0
        # vs the unconditional hold: the actual test of edge.
        t_vs_baseline = self._welch(rets, baseline)

        return {
            "Bars_Scanned": int(len(bars)),
            "Data_Start": str(bars["Date"].min().date()),
            "Data_End": str(bars["Date"].max().date()),
            "Sweeps_Triggered": int(len(trades)),
            "Sweep_Frequency_Pct": round(len(trades) / max(len(bars), 1) * 100, 2),
            "Win_Rate_Pct": round(float(wins.mean() * 100), 2),
            "Avg_Trade_Return_Pct": round(float(rets.mean() * 100), 4),
            "Profit_Factor": round(float(profit_factor), 4) if np.isfinite(profit_factor) else "inf",
            "Total_Return_Pct_Compounded": round(float((1 + pd.Series(rets)).prod() - 1) * 100, 2),
            "Ambiguous_Both_Touched_Pct": round(float(trades["Both_Touched"].mean() * 100), 2),
            "Exit_Mix": trades["Reason"].value_counts().to_dict(),
            "Baseline_Uncond_Avg_Return_Pct": round(float(baseline.mean() * 100), 4),
            "T_Stat_vs_ZERO_inflated": round(float(t_vs_zero), 4),
            "T_Stat_vs_BASELINE_true": round(float(t_vs_baseline), 4),
            "Cost_Bps_Per_Trade": COST_BPS,
            "Hold_Bars": HOLD_BARS,
        }


def main():
    print("📋 --- SPATIAL STOP-HUNT BOUNDARY EXPECTANCY (real GC=F daily OHLC) ---")
    bt = SpatialEdgeBacktester()
    try:
        results = bt.run_spatial_evaluation()
    except SpatialBacktestDataUnavailable as exc:
        print(f"🔴 [SPATIAL BACKTEST ABORTED] {exc}")
        return 1
    for k, v in results.items():
        print(f"{k}: {v}")
    t = results.get("T_Stat_vs_BASELINE_true")
    if isinstance(t, (int, float)):
        print(f"\nThe decision-relevant statistic is T_Stat_vs_BASELINE_true ({t}). "
              f"Testing against zero only measures whether gold goes up.")
        if abs(t) < 2:
            print("⚠️  |t| < 2 vs baseline: no statistically distinguishable spatial edge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
