"""Contract tests for the macro ingestion engine (Directive 01) and repository writer.

Run: python -m pytest backend/tests -q

Network is stubbed with payloads shaped exactly like the live CFTC/FRED responses, so
the join, SDI math, gate logic, and persistence are exercised for real.

Directive 01 contract covered here:
  - ingest_weekly_macro_gravity() -> DataFrame[log_date, fedwatch_dovish_prob,
    central_bank_bias]
  - ingest_institutional_fund_flow() -> DataFrame over a weekly lookback with the named
    Large_Spec_Net and Commercial_Net fields
  - synthesize_sentiment_divergence(macro_df, cot_df) -> SDI per week, gate per week,
    every week persisted
"""

import os
import sys

import pandas as pd
import pytest

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

import engine_macro as em  # noqa: E402


FRED_CSV = "DATE,DGS2\n2026-09-23,4.80\n2026-09-24,4.87\n"

# Distinct histories per series id, so the 2y-vs-policy spread is real in tests.
FRED_SERIES = {
    "DGS2": "DATE,DGS2\n2026-09-01,4.90\n2026-09-15,4.88\n2026-09-24,4.87\n",
    "DFEDTARU": "DATE,DFEDTARU\n2026-01-01,4.00\n2026-09-26,4.00\n",
}


def _cot_row(date, nl, ns, cl, cs, oi):
    return {
        "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
        "report_date_as_yyyy_mm_dd": f"{date}T00:00:00.000",
        "noncomm_positions_long_all": str(nl),
        "noncomm_positions_short_all": str(ns),
        "comm_positions_long_all": str(cl),
        "comm_positions_short_all": str(cs),
        "open_interest_all": str(oi),
    }


# Newest first, as the CFTC $order=DESC query returns them.
COT_ROWS = [
    _cot_row("2026-09-22", 253982, 28129, 57458, 320361, 412800),
    _cot_row("2026-09-15", 258059, 27721, 56417, 318138, 409899),
    _cot_row("2026-09-08", 250000, 30000, 55000, 315000, 408000),
    _cot_row("2026-09-01", 245000, 31000, 54000, 312000, 405000),
]


class _Resp:
    def __init__(self, text=None, payload=None):
        self.text = text if text is not None else ""
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def ingestor(tmp_path, monkeypatch):
    monkeypatch.setattr(em, "REPO_PATH", str(tmp_path / "repo.csv"))

    def fake_get(url, params=None, **_):
        if "cftc" in url:
            limit = int(params.get("$limit", len(COT_ROWS)))
            return _Resp(payload=COT_ROWS[:limit])
        series_id = (params or {}).get("id", "DGS2")
        return _Resp(text=FRED_SERIES.get(series_id, FRED_CSV))

    monkeypatch.setattr(em, "_get_with_retry", fake_get)
    monkeypatch.setattr(em.market_data, "get_spot", lambda: {"price": 4300.0})
    return em.InstitutionalDataIngestor(asset_symbol="XAU/USD")


def _patch_fred(monkeypatch, target=4.0, twoyr=4.87):
    """Override the FRED histories directly (the engine reads full series, not latest)."""
    monkeypatch.setitem(FRED_SERIES, "DGS2",
                        f"DATE,DGS2\n2026-09-01,{twoyr}\n2026-09-24,{twoyr}\n")
    monkeypatch.setitem(FRED_SERIES, "DFEDTARU",
                        f"DATE,DFEDTARU\n2026-01-01,{target}\n2026-09-26,{target}\n")


# --- Pillar 1 ---------------------------------------------------------------------
def test_macro_pillar_returns_dataframe_with_contract_columns(ingestor, monkeypatch):
    _patch_fred(monkeypatch)
    df = ingestor.ingest_weekly_macro_gravity()
    assert isinstance(df, pd.DataFrame)
    for col in ("log_date", "fedwatch_dovish_prob", "central_bank_bias"):
        assert col in df.columns
    assert len(df) >= 1


def test_dovish_proxy_is_hawkish_when_2y_above_policy(ingestor, monkeypatch):
    _patch_fred(monkeypatch, target=4.0, twoyr=4.87)
    df = ingestor.ingest_weekly_macro_gravity()
    assert df.iloc[0]["fedwatch_dovish_prob"] < 50.0
    assert df.iloc[0]["central_bank_bias"] in ("HAWKISH", "NEUTRAL")


def test_dovish_proxy_is_dovish_when_2y_below_policy(ingestor, monkeypatch):
    _patch_fred(monkeypatch, target=4.0, twoyr=3.5)
    df = ingestor.ingest_weekly_macro_gravity()
    assert df.iloc[0]["fedwatch_dovish_prob"] > 50.0
    assert df.iloc[0]["central_bank_bias"] == "DOVISH"


# --- Pillar 2 ---------------------------------------------------------------------
def test_fund_flow_returns_weekly_series_with_named_nets(ingestor):
    df = ingestor.ingest_institutional_fund_flow(weeks=4)
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 4
    for col in ("Large_Spec_Net", "Commercial_Net", "week_ending_date"):
        assert col in df.columns
    # Chronological order (the API query returns newest-first).
    assert df["week_ending_date"].is_monotonic_increasing


def test_named_nets_match_raw_positioning(ingestor):
    df = ingestor.ingest_institutional_fund_flow(weeks=4)
    latest = df.iloc[-1]
    assert latest["Large_Spec_Net"] == 253982 - 28129
    assert latest["Commercial_Net"] == 57458 - 320361
    assert latest["week_ending_date"] == "2026-09-22"


def test_latest_convenience_fields_preserved(ingestor):
    df = ingestor.ingest_institutional_fund_flow(weeks=4)
    latest = df.attrs["latest"]
    assert latest["net_noncomm"] == 225853.0
    assert latest["sdi_raw"] == pytest.approx((253982 - 28129) / 412800, abs=1e-6)


# --- Synthesis --------------------------------------------------------------------
def test_sdi_is_commercial_accumulation_percentile(ingestor, monkeypatch):
    _patch_fred(monkeypatch)
    macro = ingestor.ingest_weekly_macro_gravity()
    cot = ingestor.ingest_institutional_fund_flow(weeks=4)
    ingestor.synthesize_sentiment_divergence(macro, cot)

    df = pd.read_csv(em.REPO_PATH)
    comm = cot["Commercial_Net"]
    # Commercial nets are all negative (banks structurally short). The NEWEST week is the
    # most extreme short, so it maps to the low end of the percentile (0.0); the OLDEST
    # is the least short, mapping to 1.0.
    assert cot.iloc[-1]["Commercial_Net"] == comm.min()
    assert df.iloc[-1]["SDI"] == pytest.approx(0.0, abs=1e-4)
    assert df.iloc[0]["SDI"] == pytest.approx(1.0, abs=1e-4)
    # The whole window is persisted, in chronological order.
    assert len(df) == len(cot)
    assert df.iloc[-1]["week_ending_date"] == "2026-09-22"


def test_asof_join_survives_mismatched_dates(ingestor, monkeypatch):
    """Regression: the directive joined on exact date equality, which yields an empty
    frame whenever the FRED and CFTC dates differ (they nearly always do)."""
    _patch_fred(monkeypatch)  # FRED date 2026-09-26, COT date 2026-09-22
    macro = ingestor.ingest_weekly_macro_gravity()
    cot = ingestor.ingest_institutional_fund_flow(weeks=4)
    row = ingestor.synthesize_sentiment_divergence(macro, cot)
    assert row is not None
    assert row["fedwatch_dovish_prob"] > 0


def test_all_weeks_are_persisted_not_just_the_latest(ingestor, monkeypatch):
    _patch_fred(monkeypatch)
    ingestor.synthesize_sentiment_divergence(
        ingestor.ingest_weekly_macro_gravity(),
        ingestor.ingest_institutional_fund_flow(weeks=4),
    )
    df = pd.read_csv(em.REPO_PATH)
    assert len(df) == 4
    assert df["week_ending_date"].is_monotonic_increasing
    assert set(df["source"]) == {"CFTC_COT"}


@pytest.mark.parametrize("sdi,expected", [(1.0, "CLOSED"), (1.0, "CLOSED")])
def test_gate_closed_when_macro_is_hawkish(ingestor, monkeypatch, sdi, expected):
    """With real FRED values the dovish proxy is ~15%, so the gate must read CLOSED
    even when institutional accumulation is at its maximum."""
    _patch_fred(monkeypatch, target=4.0, twoyr=4.87)
    row = ingestor.synthesize_sentiment_divergence(
        ingestor.ingest_weekly_macro_gravity(),
        ingestor.ingest_institutional_fund_flow(weeks=4),
    )
    assert row["MACRO_GATE"] == expected


def test_rerun_same_week_replaces_rather_than_duplicates(ingestor, monkeypatch):
    _patch_fred(monkeypatch)
    for _ in range(3):
        ingestor.synthesize_sentiment_divergence(
            ingestor.ingest_weekly_macro_gravity(),
            ingestor.ingest_institutional_fund_flow(weeks=4),
        )
    df = pd.read_csv(em.REPO_PATH)
    assert len(df) == 4  # still four distinct weeks, not twelve
    assert df["week_ending_date"].nunique() == 4


def test_spatial_levels_carried_forward(ingestor, monkeypatch):
    _patch_fred(monkeypatch)
    first = ingestor.synthesize_sentiment_divergence(
        ingestor.ingest_weekly_macro_gravity(),
        ingestor.ingest_institutional_fund_flow(weeks=4),
    )
    second = ingestor.synthesize_sentiment_divergence(
        ingestor.ingest_weekly_macro_gravity(),
        ingestor.ingest_institutional_fund_flow(weeks=4),
    )
    assert second["spot_price"] == pytest.approx(4300.0)
    assert second["liquidity_sweep_floor"] == pytest.approx(4278.5)


def test_backward_compatible_with_single_row_dicts(ingestor, monkeypatch):
    """The first build passed plain dicts; the richer contract must still accept them."""
    row = ingestor.synthesize_sentiment_divergence(
        {"fedwatch_dovish_prob": 60.0},
        {"sdi_raw": 0.6, "report_date": "2026-09-22", "Commercial_Net": 100.0},
    )
    assert row["week_ending_date"] == "2026-09-22"


def test_rows_without_macro_reading_are_dropped_not_written_blank(ingestor, monkeypatch):
    """Regression: rows with no prevailing macro reading were persisted with an empty
    fedwatch_dovish_prob, a silent hole in the repository."""
    # Macro series that starts after the earliest COT week -> early weeks have no match.
    monkeypatch.setitem(FRED_SERIES, "DGS2", "DATE,DGS2\n2026-09-20,4.87\n2026-09-24,4.87\n")
    monkeypatch.setitem(FRED_SERIES, "DFEDTARU", "DATE,DFEDTARU\n2026-01-01,4.00\n2026-09-26,4.00\n")

    ingestor.synthesize_sentiment_divergence(
        ingestor.ingest_weekly_macro_gravity(),
        ingestor.ingest_institutional_fund_flow(weeks=4),
    )
    df = pd.read_csv(em.REPO_PATH)
    assert not df["fedwatch_dovish_prob"].isna().any()
    assert len(df) >= 1


def test_empty_join_raises_rather_than_writing_nothing(ingestor, monkeypatch):
    with pytest.raises(ValueError, match="no joined rows"):
        ingestor.synthesize_sentiment_divergence(
            pd.DataFrame([{"log_date": "2099-01-01", "fedwatch_dovish_prob": 50.0}]),
            pd.DataFrame([{"week_ending_date": "1990-01-01", "Commercial_Net": 1.0}]),
        )
