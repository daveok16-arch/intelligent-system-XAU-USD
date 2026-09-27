"""
SYSTEM MODULE: VECTORIZED HISTORICAL BACKTEST ENGINE
ROLE: EVALUATE MACRO GATE + SENTIMENT DIVERGENCE EXPECTANCY USING REAL HISTORY

The production gate is: enter long gold when MACRO_GATE == OPEN, i.e. when
(SDI > 0.50 AND dovish-proxy > 0.50). This module asks whether that rule has ever
had positive expectancy.

Corrections vs the Directive 13 draft, each proven by running it:
  1. The draft read price from a 'Close' column in spatial_boundaries_repository.csv.
     That file has no such column, so it silently fell into an `np.random.normal`
     branch and reported a Sharpe ratio computed from RANDOM NUMBERS. This module
     fetches real prices and REFUSES to run without them.
  2. The draft read "4,446 macro lines and 52 weeks" from disk. On disk there were
     4 macro rows and 49 spatial rows, so the merge produced 49 rows with 18 macro
     matches and ZERO signals -- every metric came back 0.0. The real history is
     fetchable (CFTC back to 1986, gold prices from 2000); this module uses it.
  3. The draft's SDI used a full-sample min/max percentile -- that is look-ahead bias
     (it uses the future to normalise the past). This uses a ROLLING 52-week percentile.
  4. No benchmark. A Sharpe in isolation is uninterpretable without buy-and-hold.
  5. No costs. An untraded or cost-free strategy flatters itself.

Measured honestly, including when the answer is "this rule barely trades".
"""

import os
import sys

import numpy as np
import pandas as pd
import requests
import yfinance as yf

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

_DATA_DIR = os.getenv("DATA_DIR", os.path.join(_PROJECT_DIR, "data"))

COT_URL = os.getenv("COT_URL", "https://publicreporting.cftc.gov/resource/6dca-aqww.json")
FRED_URL = os.getenv("FRED_URL", "https://fred.stlouisfed.org/graph/fredgraph.csv")
GOLD_COT_CONTRACT = os.getenv("GOLD_COT_CONTRACT", "088691")

SDI_WINDOW = int(os.getenv("SDI_REFERENCE_WEEKS", "52"))   # rolling weeks for percentile
TRADING_WEEKS = 52                                          # annualisation basis
RISK_FREE_RATE = float(os.getenv("BACKTEST_RISK_FREE", "0.04"))
COST_BPS_PER_TRADE = float(os.getenv("BACKTEST_COST_BPS", "5.0"))  # round-trip cost


class BacktestDataUnavailable(RuntimeError):
    """Real history could not be assembled; a backtest must not be simulated on noise."""


class VectorizedBacktester:
    def __init__(self, data_dir=None, ticker="GC=F", start=None):
        self.data_dir = data_dir or _DATA_DIR
        self.ticker = ticker
        self.start = start or "2000-09-01"  # gold price history begins Aug 2000
        self.cache_path = os.path.join(self.data_dir, "backtest_history_cache.csv")

    # --- real history -----------------------------------------------------------
    def _fetch_cot_history(self):
        params = {
            "$where": f"cftc_contract_market_code='{GOLD_COT_CONTRACT}'",
            "$order": "report_date_as_yyyy_mm_dd ASC",
            "$select": ("report_date_as_yyyy_mm_dd,noncomm_positions_long_all,"
                        "noncomm_positions_short_all,comm_positions_long_all,"
                        "comm_positions_short_all,open_interest_all"),
            "$limit": "50000",
        }
        resp = requests.get(COT_URL, params=params, timeout=60)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            raise BacktestDataUnavailable("CFTC COT history returned no rows")
        df = pd.DataFrame(rows)
        df["Target_Date"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"], errors="coerce")
        for col in ("noncomm_positions_long_all", "noncomm_positions_short_all",
                    "comm_positions_long_all", "comm_positions_short_all", "open_interest_all"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["Commercial_Net"] = df["comm_positions_long_all"] - df["comm_positions_short_all"]
        return df[["Target_Date", "Commercial_Net", "open_interest_all"]].dropna().sort_values("Target_Date")

    def _fetch_rate_history(self):
        def series(series_id):
            text = requests.get(FRED_URL, params={"id": series_id}, timeout=60).text
            rows = []
            for line in text.strip().splitlines()[1:]:
                if "," not in line:
                    continue
                d, v = line.split(",", 1)
                v = v.strip()
                if v in ("", ".", "NaN"):
                    continue
                rows.append({"date": d.strip(), "value": float(v)})
            return pd.DataFrame(rows).rename(columns={"value": series_id})

        policy = series("DFEDTARU").rename(columns={"DFEDTARU": "policy_rate"})
        twoyr = series("DGS2").rename(columns={"DGS2": "two_year"})
        merged = pd.merge_asof(
            twoyr.assign(_d=pd.to_datetime(twoyr["date"])).sort_values("_d"),
            policy.assign(_d=pd.to_datetime(policy["date"])).sort_values("_d"),
            on="_d", direction="backward",
        ).dropna(subset=["policy_rate"])
        merged["Target_Date"] = merged["_d"]
        return merged[["Target_Date", "policy_rate", "two_year"]].sort_values("Target_Date")

    def _fetch_prices(self):
        df = yf.download(self.ticker, start=self.start, interval="1d",
                         progress=False, auto_adjust=False)
        if df is None or df.empty:
            raise BacktestDataUnavailable(f"no price history returned for {self.ticker}")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.reset_index()
        date_col = "Date" if "Date" in df.columns else df.columns[0]
        df = df.rename(columns={date_col: "Target_Date"})
        df = df[["Target_Date", "Close"]].dropna()
        if df["Close"].isna().all():
            raise BacktestDataUnavailable("price history contained no usable closes")
        return df.sort_values("Target_Date")

    def load_history(self):
        """Real merged weekly history. Raises rather than synthesising prices."""
        try:
            cot = self._fetch_cot_history()
            rates = self._fetch_rate_history()
            prices = self._fetch_prices()
        except BacktestDataUnavailable:
            raise
        except Exception as exc:
            raise BacktestDataUnavailable(f"could not assemble real history: {type(exc).__name__}: {exc}")

        # Weekly price bars for a weekly strategy. Normalise every date key to a common
        # resolution: merge_asof requires identical datetime dtypes across frames.
        def _norm(series):
            return pd.to_datetime(series).dt.tz_localize(None).astype("datetime64[ns]")

        prices = prices.assign(Target_Date=_norm(prices["Target_Date"]))
        cot = cot.assign(Target_Date=_norm(cot["Target_Date"]))
        rates = rates.assign(Target_Date=_norm(rates["Target_Date"]))

        weekly = (prices.set_index("Target_Date")["Close"]
                  .resample("W-FRI").last().dropna().reset_index())

        merged = pd.merge_asof(weekly.sort_values("Target_Date"), cot, on="Target_Date",
                               direction="backward")
        merged = pd.merge_asof(merged.sort_values("Target_Date"), rates, on="Target_Date",
                               direction="backward")
        merged = merged.dropna(subset=["Close", "Commercial_Net", "policy_rate", "two_year"])
        if len(merged) < SDI_WINDOW + 10:
            raise BacktestDataUnavailable(f"insufficient overlapping history: {len(merged)} rows")
        return merged.reset_index(drop=True)

    # --- signals (look-ahead free) -----------------------------------------------
    def build_signals(self, df):
        required = {"Target_Date", "Close", "Commercial_Net", "policy_rate", "two_year"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"history missing required columns: {sorted(missing)}")
        if len(df) < SDI_WINDOW + 1:
            raise ValueError(
                f"insufficient history to build signals: {len(df)} rows, "
                f"need at least {SDI_WINDOW + 1} for a {SDI_WINDOW}-week rolling percentile"
            )
        out = df.copy()

        # Rolling percentile: each week is ranked only against the trailing window.
        # A full-sample min/max would use the future to normalise the past.
        roll = out["Commercial_Net"].rolling(SDI_WINDOW, min_periods=SDI_WINDOW)
        out["SDI"] = ((out["Commercial_Net"] - roll.min()) / (roll.max() - roll.min())).clip(0, 1)

        # Dovish proxy: logistic on 2y-minus-policy, identical to production.
        spread = out["two_year"] - out["policy_rate"]
        out["dovish"] = 1.0 / (1.0 + np.exp(spread / 0.5))

        out["MACRO_GATE"] = np.where((out["SDI"] > 0.50) & (out["dovish"] > 0.50), "OPEN", "CLOSED")

        # Long gold while the gate is open. Shift by one bar: the decision is made on
        # the close of week t and executed over week t+1.
        out["Signal"] = np.where(out["MACRO_GATE"] == "OPEN", 1.0, 0.0)
        out["Execution_Signal"] = out["Signal"].shift(1).fillna(0.0)

        out["Market_Returns"] = out["Close"].pct_change().fillna(0.0)

        # Transaction costs charged whenever exposure changes.
        turnover = out["Execution_Signal"].diff().abs().fillna(out["Execution_Signal"].abs())
        out["Costs"] = turnover * (COST_BPS_PER_TRADE / 10000.0)
        out["Strategy_Returns"] = out["Market_Returns"] * out["Execution_Signal"] - out["Costs"]
        return out

    # --- metrics -----------------------------------------------------------------
    @staticmethod
    def _metrics(returns):
        equity = 100000.0 * (1 + returns).cumprod()
        peak = equity.cummax()
        drawdown = (equity - peak) / peak
        ann_return = returns.mean() * TRADING_WEEKS
        ann_vol = returns.std() * np.sqrt(TRADING_WEEKS)
        sharpe = ((ann_return - RISK_FREE_RATE) / ann_vol) if ann_vol > 0 else 0.0
        return {
            "final_value": float(equity.iloc[-1]),
            "total_return_pct": float((equity.iloc[-1] / 100000.0 - 1) * 100),
            "annualized_sharpe": float(sharpe),
            "max_drawdown_pct": float(drawdown.min() * 100),
            "annualized_vol_pct": float(ann_vol * 100),
        }

    def execute_historical_analysis(self, df=None):
        data = self.build_signals(df if df is not None else self.load_history())

        strat = self._metrics(data["Strategy_Returns"])
        bench = self._metrics(data["Market_Returns"])

        exposure = float(data["Execution_Signal"].mean())
        trades = int((data["Execution_Signal"].diff().abs() > 0).sum())

        # Selection skill: do gate-open weeks actually perform better than the rest?
        # This is the cleanest single test of whether the signal carries information.
        open_weeks = data.loc[data["MACRO_GATE"] == "OPEN", "Market_Returns"]
        closed_weeks = data.loc[data["MACRO_GATE"] == "CLOSED", "Market_Returns"]
        open_mean = float(open_weeks.mean() * 100) if len(open_weeks) else float("nan")
        closed_mean = float(closed_weeks.mean() * 100) if len(closed_weeks) else float("nan")

        # Welch t-statistic for the difference of means, computed with numpy so no
        # scipy dependency is required. |t| < ~2 means the "edge" is indistinguishable
        # from noise at conventional thresholds.
        t_stat = float("nan")
        if len(open_weeks) > 1 and len(closed_weeks) > 1:
            se = np.sqrt(open_weeks.var(ddof=1) / len(open_weeks)
                         + closed_weeks.var(ddof=1) / len(closed_weeks))
            if se > 0:
                t_stat = float((open_weeks.mean() - closed_weeks.mean()) / se)

        return {
            "Data_Start": str(data["Target_Date"].min().date()),
            "Data_End": str(data["Target_Date"].max().date()),
            "Total_Trading_Weeks": int(len(data)),
            "Gate_Open_Weeks": int((data["MACRO_GATE"] == "OPEN").sum()),
            "Exposure_Pct": round(exposure * 100, 2),
            "Trades": trades,
            "Gate_Open_Mean_Week_Return_Pct": round(open_mean, 4),
            "Gate_Closed_Mean_Week_Return_Pct": round(closed_mean, 4),
            "Selection_Edge_Pct_Per_Week": round(open_mean - closed_mean, 4),
            "Selection_Edge_t_stat": round(t_stat, 4) if t_stat == t_stat else None,
            "Final_Portfolio_Value": strat["final_value"],
            "Strategy_Total_Return_Pct": round(strat["total_return_pct"], 2),
            "Annualized_Sharpe_Ratio": round(strat["annualized_sharpe"], 4),
            "Max_Drawdown_Percentage": round(strat["max_drawdown_pct"], 2),
            "Benchmark_BuyHold_Return_Pct": round(bench["total_return_pct"], 2),
            "Benchmark_BuyHold_Sharpe": round(bench["annualized_sharpe"], 4),
            "Benchmark_BuyHold_MDD_Pct": round(bench["max_drawdown_pct"], 2),
            "Cost_Bps_Per_Trade": COST_BPS_PER_TRADE,
        }


def main():
    bt = VectorizedBacktester()
    print("📋 --- HISTORICAL EDGE EVALUATION (real GC=F prices, real CFTC/FRED history) ---")
    try:
        metrics = bt.execute_historical_analysis()
    except BacktestDataUnavailable as exc:
        print(f"🔴 [BACKTEST ABORTED] {exc}")
        print("    No result is reported rather than a simulated one.")
        return 1
    for key, val in metrics.items():
        print(f"{key}: {val}")
    if metrics["Gate_Open_Weeks"] == 0:
        print("\n⚠️  The gate never opened over this history: there is no strategy to evaluate.")
    elif metrics["Trades"] < 20:
        print(f"\n⚠️  Only {metrics['Trades']} trades: sample too small for statistical claims.")
    t = metrics.get("Selection_Edge_t_stat")
    if t is not None and abs(t) < 2:
        print(f"\n⚠️  Selection edge t={t} is within noise (|t| < 2): the gate's apparent "
              f"skill is not statistically distinguishable from chance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
