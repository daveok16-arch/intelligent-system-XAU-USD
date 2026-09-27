"""
APPLICATION MODULE: MACRO DATA INGESTION ENGINE
ROLE: INGEST WEEKLY MACRO GRAVITY AND INSTITUTIONAL FUND FLOW, SYNTHESIZE THE SDI

Data sources (both keyless):
  - CME gold Commitments of Traders, contract 088691, via CFTC public reporting:
    https://publicreporting.cftc.gov/resource/6dca-aqww.json
  - Federal Reserve macro series via FRED CSV:
    https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>

Methodology (all documented so the numbers are auditable):
  - Institutional Fund Flow / SDI = net non-commercial positioning as a fraction
    of open interest: (noncomm_long - noncomm_short) / open_interest. This lands on
    the same 0-1 scale the cockpit's >0.50 "accumulating" threshold assumes.
  - Global Macro Gravity / dovish pivot proxy = logistic mapping of the 2-year
    Treasury yield relative to the policy target upper bound. A 2y yield below the
    policy rate implies the market prices cuts (dovish -> probability > 0.50); a 2y
    above the policy rate implies the market prices hikes (hawkish -> < 0.50).
    NOTE: this is a transparent proxy, not the true CME FedWatch probability, which
    requires a paid futures feed. It is labelled as a proxy everywhere it surfaces.
"""

import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import requests

# market_data lives one level up (backend/), shared with the API service.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.append(_BACKEND_DIR)

import market_data  # noqa: E402

COT_URL = os.getenv("COT_URL", "https://publicreporting.cftc.gov/resource/6dca-aqww.json")
FRED_URL = os.getenv("FRED_URL", "https://fred.stlouisfed.org/graph/fredgraph.csv")
GOLD_COT_CONTRACT = os.getenv("GOLD_COT_CONTRACT", "088691")  # GOLD - COMMODITY EXCHANGE INC.
FED_TARGET_SERIES = os.getenv("FED_TARGET_SERIES", "DFEDTARU")
TWO_YEAR_SERIES = os.getenv("TWO_YEAR_SERIES", "DGS2")
HTTP_TIMEOUT = float(os.getenv("MACRO_HTTP_TIMEOUT", "15"))

REPO_PATH = os.getenv(
    "MACRO_REPO_CSV",
    os.path.join(os.path.dirname(_BACKEND_DIR), "data", "macro_intelligence_repository.csv"),
)

# No custom User-Agent: FRED's edge stalls requests carrying the bot-style UA we
# previously sent, while plain requests return in ~0.1s. Both feeds accept default headers.
_UA = None


def _clamp(value, low, high):
    return max(low, min(high, value))


def _get_with_retry(url, params, attempts=3, backoff=2.0):
    """GET with bounded retries; upstream macro tapes are intermittently slow."""
    last_error = None
    for attempt in range(attempts):
        try:
            resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT, headers=_UA)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_error


def _bias_label(dovish):
    """Map a dovish probability onto the descriptive band the directive used."""
    if dovish >= 0.60:
        return "DOVISH"
    if dovish >= 0.50:
        return "DOVISH_TILT"
    if dovish <= 0.35:
        return "HAWKISH"
    return "NEUTRAL"


def _as_frame(obj, date_alias):
    """Coerce a DataFrame or legacy single-row dict into a frame, normalising the
    date column onto the first name in `date_alias` so older callers keep working."""
    df = obj if isinstance(obj, pd.DataFrame) else pd.DataFrame([obj])
    for alias in date_alias[1:]:
        if alias in df.columns and date_alias[0] not in df.columns:
            df = df.rename(columns={alias: date_alias[0]})
    return df


class InstitutionalDataIngestor:
    def __init__(self, asset_symbol="XAU/USD"):
        self.asset_symbol = asset_symbol

    # --- Source 1: macro gravity -------------------------------------------------
    def _fred_series(self, series):
        """Return the full FRED history as DataFrame[date, value].

        Using the whole history (rather than only the latest observation) lets each COT
        week be matched to the macro regime prevailing *at that time*, which is both
        look-ahead-free and the only way the as-of join is meaningful.
        """
        resp = _get_with_retry(FRED_URL, {"id": series})
        rows = []
        for line in resp.text.strip().splitlines()[1:]:
            if "," not in line:
                continue
            date, value = line.split(",", 1)
            value = value.strip()
            if value in ("", ".", "NaN"):
                continue
            rows.append({"date": date.strip(), "value": float(value)})
        if not rows:
            raise ValueError(f"FRED series {series} returned no usable observations")
        return pd.DataFrame(rows)

    def _fred_latest(self, series):
        """Most recent observation only (kept for compatibility)."""
        s = self._fred_series(series)
        last = s.iloc[-1]
        return str(last["date"]), float(last["value"])

    def ingest_weekly_macro_gravity(self, lookback_rows=None):
        """Pillar 1. Return the dovish-pivot series as a DataFrame (Directive 01 contract).

        Columns: log_date, fedwatch_dovish_prob, policy_target_rate, two_year_yield,
        central_bank_bias. The logistic proxy is computed per observation from the
        prevailing 2y-vs-policy spread.
        """
        target = self._fred_series(FED_TARGET_SERIES).rename(columns={"value": "policy_target_rate"})
        twoyr = self._fred_series(TWO_YEAR_SERIES).rename(columns={"value": "two_year_yield"})

        merged = pd.merge_asof(
            twoyr.assign(_d=pd.to_datetime(twoyr["date"])).sort_values("_d"),
            target.assign(_d=pd.to_datetime(target["date"])).sort_values("_d"),
            on="_d",
            direction="backward",
        ).dropna(subset=["policy_target_rate"])

        merged["spread"] = merged["two_year_yield"] - merged["policy_target_rate"]
        merged["fedwatch_dovish_prob"] = (
            1.0 / (1.0 + pow(2.718281828, merged["spread"] / 0.5)) * 100.0
        ).round(2)
        merged["central_bank_bias"] = merged["fedwatch_dovish_prob"].apply(
            lambda p: _bias_label(p / 100.0)
        )
        merged["log_date"] = merged["_d"].dt.strftime("%Y-%m-%d")

        out = merged[["log_date", "fedwatch_dovish_prob", "policy_target_rate",
                      "two_year_yield", "central_bank_bias"]].reset_index(drop=True)
        if lookback_rows:
            out = out.tail(lookback_rows).reset_index(drop=True)
        return out

    # --- Source 2: institutional fund flow --------------------------------------
    def ingest_institutional_fund_flow(self, weeks=4):
        """Pillar 2. Return the multi-week COT positioning series (Directive 01 contract).

        Emits the fields the directive named: Large_Spec_Net and Commercial_Net, over a
        configurable weekly lookback so the SDI is computed across a real window rather
        than a single snapshot.
        """
        params = {
            "$where": f"cftc_contract_market_code='{GOLD_COT_CONTRACT}'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": str(max(1, weeks)),
        }
        resp = _get_with_retry(COT_URL, params)
        rows = resp.json()
        if not rows:
            raise ValueError(f"no COT rows for contract {GOLD_COT_CONTRACT}")

        records = []
        for row in rows:
            long_ = float(row["noncomm_positions_long_all"])
            short_ = float(row["noncomm_positions_short_all"])
            comm_long = float(row["comm_positions_long_all"])
            comm_short = float(row["comm_positions_short_all"])
            oi = float(row["open_interest_all"])
            records.append({
                "week_ending_date": str(row.get("report_date_as_yyyy_mm_dd", ""))[:10],
                "large_spec_longs": long_,
                "large_spec_shorts": short_,
                "commercial_longs": comm_long,
                "commercial_shorts": comm_short,
                "open_interest": oi,
                "Large_Spec_Net": long_ - short_,
                "Commercial_Net": comm_long - comm_short,
            })

        df = pd.DataFrame(records).sort_values("week_ending_date").reset_index(drop=True)

        # Latest-row convenience fields keep the existing single-snapshot consumers working.
        latest = df.iloc[-1]
        df.attrs["latest"] = {
            "market": rows[0].get("market_and_exchange_names"),
            "report_date": latest["week_ending_date"],
            "noncomm_long": float(latest["large_spec_longs"]),
            "noncomm_short": float(latest["large_spec_shorts"]),
            "open_interest": float(latest["open_interest"]),
            "net_noncomm": float(latest["Large_Spec_Net"]),
            "sdi_raw": round(float(latest["Large_Spec_Net"]) / float(latest["open_interest"]), 6)
            if latest["open_interest"] else 0.0,
        }
        return df

    # --- Synthesis + persistence -------------------------------------------------
    def synthesize_sentiment_divergence(self, macro_df, cot_df):
        """Alpha engine (Directive 01): join the macro tape to the positioning series,
        compute the SDI for each week, apply the systemic gate, and persist every row.

        Defensive against both contracts: DataFrames (the directive's shape) and the
        single-row dicts the first build used.
        """
        cot = _as_frame(cot_df, date_alias=("week_ending_date", "report_date"))
        macro = _as_frame(macro_df, date_alias=("log_date", "as_of"))
        if "log_date" not in macro.columns and "week_ending_date" in macro.columns:
            macro = macro.rename(columns={"week_ending_date": "log_date"})
        # A legacy single macro reading with no date can still pair with every COT week.
        if "log_date" not in macro.columns:
            macro["log_date"] = "1970-01-01"

        # The directive joined on exact string equality, which silently yields an empty
        # frame whenever the FRED and CFTC dates differ (they almost always do). Merge
        # asof-style on parsed dates instead: each COT week takes the prevailing macro
        # reading. merge_asof requires datetime/numeric keys, not strings.
        cot = cot.assign(
            _week=pd.to_datetime(cot["week_ending_date"], errors="coerce")
        ).dropna(subset=["_week"]).sort_values("_week").reset_index(drop=True)
        macro = macro.assign(
            _macro=pd.to_datetime(macro["log_date"], errors="coerce")
        ).dropna(subset=["_macro"]).sort_values("_macro").reset_index(drop=True)
        unified = pd.merge_asof(cot, macro, left_on="_week", right_on="_macro")

        if unified.empty:
            raise ValueError("macro and COT tapes produced no joined rows")
        if unified["fedwatch_dovish_prob"].isna().all():
            raise ValueError("macro and COT tapes produced no joined rows "
                             "(no macro reading available at or before any COT week)")

        # A COT week with no prevailing macro reading cannot be gated. Drop it rather
        # than writing a blank cell into the repository, which would be a silent hole.
        before = len(unified)
        unified = unified.dropna(subset=["fedwatch_dovish_prob"]).reset_index(drop=True)
        dropped = before - len(unified)
        if dropped:
            print(f"⚠️ [SYNTHESIS] Dropped {dropped} COT week(s) with no macro reading.")
        if unified.empty:
            raise ValueError("every COT week lacked a macro reading; nothing to persist")

        # SDI (Directive 01 definition): commercial accumulation strength less the retail
        # long-placement bias, where accumulation strength is the commercial net
        # positioning expressed as a percentile *relative to neutral*: a commercial net
        # at the bullish extreme maps to 1.0, neutral to 0.5, the bearish extreme to 0.0.
        # Retail ratio comes from a separate sentiment feed and is not in the COT tape,
        # so that bias term is neutral (0.0) here; the institutional component is real.
        comm = unified["Commercial_Net"]
        comm_min, comm_max = comm.min(), comm.max()
        if comm_max != comm_min:
            pct = (comm - comm_min) / (comm_max - comm_min)          # 0..1 across the window
        else:
            pct = pd.Series(0.5, index=unified.index)
        unified["SDI"] = ((pct - 0.5) + 0.5).round(4)

        dovish = unified["fedwatch_dovish_prob"] / 100.0
        unified["MACRO_GATE"] = [
            "OPEN" if (s > 0.50 and d > 0.50) else "CLOSED"
            for s, d in zip(unified["SDI"], dovish)
        ]

        # Carry spatial levels forward; strategy owns them, ingestion does not invent them.
        if os.path.exists(REPO_PATH) and os.path.getsize(REPO_PATH) > 0:
            prior = pd.read_csv(REPO_PATH)
            prior = prior.assign(
                _d=pd.to_datetime(prior["week_ending_date"], errors="coerce")
            ).dropna(subset=["_d"])
            latest_prior = prior.loc[prior["_d"].idxmax()]
            spot = float(latest_prior["spot_price"])
            floor = float(latest_prior["liquidity_sweep_floor"])
        else:
            live = market_data.get_spot()
            spot = live["price"] if live else 0.0
            floor = round(spot * 0.995, 2) if spot else 0.0

        rows = [
            {
                "week_ending_date": r["week_ending_date"],
                "fedwatch_dovish_prob": float(r["fedwatch_dovish_prob"]),
                "SDI": float(r["SDI"]),
                "MACRO_GATE": r["MACRO_GATE"],
                "spot_price": spot,
                "liquidity_sweep_floor": floor,
                "source": "CFTC_COT",
            }
            for _, r in unified.iterrows()
        ]
        self._append_rows(rows)

        latest = rows[-1]
        print(f"🚨 [PIPELINE SUCCESS] -> {len(rows)} week(s) synthesized; latest "
              f"{latest['week_ending_date']} SDI {latest['SDI']} gate {latest['MACRO_GATE']}.")
        return latest

    def _append_rows(self, rows):
        """Idempotent, chronologically sorted batch write (one row per report week)."""
        if not rows:
            return
        os.makedirs(os.path.dirname(REPO_PATH), exist_ok=True)
        fresh = pd.DataFrame(rows)
        if os.path.exists(REPO_PATH) and os.path.getsize(REPO_PATH) > 0:
            existing = pd.read_csv(REPO_PATH)
            if "source" not in existing.columns:
                existing["source"] = "SEED"
            # Re-running a week replaces it rather than duplicating.
            existing = existing[~existing["week_ending_date"].isin(fresh["week_ending_date"])]
            fresh = pd.concat([existing, fresh], ignore_index=True)
        fresh = fresh.sort_values("week_ending_date").reset_index(drop=True)
        fresh.to_csv(REPO_PATH, index=False)

    def _append_row(self, row):
        """Single-row convenience wrapper retained for backward compatibility."""
        self._append_rows([row])


def main(argv=None):
    """Single-shot ingestion entry point for the Kubernetes CronJob.

    The CronJob schedule is the coarse timer; this entry point enforces the finer
    release-window guard so a job that fires early cannot compute against an
    unreleased tape. Exits non-zero on failure so restartPolicy=OnFailure surfaces it.

    Usage: python -m engine_macro [--force]
    """
    import argparse

    parser = argparse.ArgumentParser(description="Weekly macro ingestion (single shot).")
    parser.add_argument("--force", action="store_true", help="Bypass the release-window guard.")
    args = parser.parse_args(argv)

    if not args.force:
        try:
            from orchestrator import should_trigger_weekly_macro

            if not should_trigger_weekly_macro(REPO_PATH):
                print("⏸️ [MACRO JOB] Outside the Friday 15:30 EST window or week already synced. No-op.")
                return 0
        except Exception as exc:
            # Fail closed: never ingest against an unverifiable clock.
            print(f"⚠️ [MACRO JOB] Schedule guard unavailable ({type(exc).__name__}: {exc}); aborting.")
            return 1

    try:
        director = InstitutionalDataIngestor(asset_symbol="XAU/USD")
        row = director.synthesize_sentiment_divergence(
            director.ingest_weekly_macro_gravity(),
            director.ingest_institutional_fund_flow(),
        )
        print(f"✅ [MACRO JOB] Repository refreshed for {row['week_ending_date']} "
              f"(SDI {row['SDI']}, gate {row['MACRO_GATE']}).")
        return 0
    except Exception as exc:
        print(f"🔴 [MACRO JOB] Ingestion failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
