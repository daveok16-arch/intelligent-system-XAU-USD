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


class InstitutionalDataIngestor:
    def __init__(self, asset_symbol="XAU/USD"):
        self.asset_symbol = asset_symbol

    # --- Source 1: macro gravity -------------------------------------------------
    def _fred_latest(self, series):
        resp = _get_with_retry(FRED_URL, {"id": series})
        rows = [line for line in resp.text.strip().splitlines() if line and "," in line]
        for line in reversed(rows[1:]):
            date, value = line.split(",", 1)
            value = value.strip()
            if value not in ("", ".", "NaN"):
                return date.strip(), float(value)
        raise ValueError(f"FRED series {series} returned no usable observations")

    def ingest_weekly_macro_gravity(self):
        """Return the dovish-pivot proxy derived from the FRED policy path."""
        target_date, target_rate = self._fred_latest(FED_TARGET_SERIES)
        twoyr_date, twoyr_rate = self._fred_latest(TWO_YEAR_SERIES)

        # Logistic on the 2y-policy spread; 0.5pp spread ~= one logistic unit.
        spread = twoyr_rate - target_rate
        dovish = 1.0 / (1.0 + pow(2.718281828, spread / 0.5))
        dovish = _clamp(dovish, 0.0, 1.0)

        return {
            "asset_symbol": self.asset_symbol,
            "fedwatch_dovish_prob": round(dovish * 100.0, 2),
            "policy_target_rate": target_rate,
            "two_year_yield": twoyr_rate,
            "as_of": target_date,
            "series_dates": {"target": target_date, "two_year": twoyr_date},
            "methodology": "logistic proxy on 2y minus policy target (not CME FedWatch)",
        }

    # --- Source 2: institutional fund flow --------------------------------------
    def ingest_institutional_fund_flow(self):
        """Return net non-commercial positioning from the CME gold COT report."""
        params = {
            "$where": f"cftc_contract_market_code='{GOLD_COT_CONTRACT}'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": "2",
        }
        resp = _get_with_retry(COT_URL, params)
        rows = resp.json()
        if not rows:
            raise ValueError(f"no COT rows for contract {GOLD_COT_CONTRACT}")

        latest = rows[0]
        long_ = float(latest["noncomm_positions_long_all"])
        short_ = float(latest["noncomm_positions_short_all"])
        oi = float(latest["open_interest_all"])
        net = long_ - short_

        prior_net = None
        if len(rows) > 1:
            prior_net = float(rows[1]["noncomm_positions_long_all"]) - float(
                rows[1]["noncomm_positions_short_all"]
            )

        return {
            "market": latest.get("market_and_exchange_names"),
            "report_date": str(latest.get("report_date_as_yyyy_mm_dd", ""))[:10],
            "noncomm_long": long_,
            "noncomm_short": short_,
            "open_interest": oi,
            "net_noncomm": net,
            "net_change": (net - prior_net) if prior_net is not None else None,
            "sdi_raw": round(net / oi, 6) if oi else 0.0,
        }

    # --- Synthesis + persistence -------------------------------------------------
    def synthesize_sentiment_divergence(self, macro_df, cot_df):
        """Combine both tapes into one repository row and append it to the CSV."""
        sdi = float(cot_df["sdi_raw"])
        dovish = float(macro_df["fedwatch_dovish_prob"]) / 100.0

        # Gate opens only when fund flow is accumulating AND macro has a dovish tailwind.
        gate = "OPEN" if (sdi > 0.50 and dovish > 0.50) else "CLOSED"

        # Spatial levels are strategy-owned; ingestion carries them forward.
        if os.path.exists(REPO_PATH) and os.path.getsize(REPO_PATH) > 0:
            prior = pd.read_csv(REPO_PATH)
            spot = float(prior.iloc[-1]["spot_price"])
            floor = float(prior.iloc[-1]["liquidity_sweep_floor"])
        else:
            live = market_data.get_spot()
            spot = live["price"] if live else 0.0
            floor = round(spot * 0.995, 2) if spot else 0.0

        row = {
            "week_ending_date": cot_df["report_date"] or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "fedwatch_dovish_prob": round(dovish * 100.0, 2),
            "SDI": round(sdi, 4),
            "MACRO_GATE": gate,
            "spot_price": spot,
            "liquidity_sweep_floor": floor,
            "source": "CFTC_COT",
        }
        self._append_row(row)
        return row

    def _append_row(self, row):
        os.makedirs(os.path.dirname(REPO_PATH), exist_ok=True)
        df = pd.DataFrame([row])
        if os.path.exists(REPO_PATH) and os.path.getsize(REPO_PATH) > 0:
            existing = pd.read_csv(REPO_PATH)
            if "source" not in existing.columns:
                existing["source"] = "SEED"
            # Idempotent: a re-run for the same week replaces rather than duplicates.
            existing = existing[existing["week_ending_date"] != row["week_ending_date"]]
            df = pd.concat([existing, df], ignore_index=True)
        # Chronological order keeps the file readable; readers still select by max date.
        df = df.sort_values("week_ending_date").reset_index(drop=True)
        df.to_csv(REPO_PATH, index=False)


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
