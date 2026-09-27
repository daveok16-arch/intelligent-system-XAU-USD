"""Contract tests for the macro ingestion engine and repository writer.

Run: python -m pytest backend/tests -q
Network calls are stubbed with canned payloads matching the live source schemas,
so the math and persistence logic are exercised for real without hitting
CFTC/FRED on every run.
"""

import os
import sys

import pandas as pd
import pytest

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

import engine_macro as em  # noqa: E402


FRED_CSV = "DATE,DGS2\n2026-09-23,4.80\n2026-09-24,4.87\n"
COT_ROWS = [
    {
        "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
        "report_date_as_yyyy_mm_dd": "2026-09-22T00:00:00.000",
        "noncomm_positions_long_all": "253982",
        "noncomm_positions_short_all": "28129",
        "open_interest_all": "412800",
    },
    {
        "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
        "report_date_as_yyyy_mm_dd": "2026-09-15T00:00:00.000",
        "noncomm_positions_long_all": "258059",
        "noncomm_positions_short_all": "27721",
        "open_interest_all": "409899",
    },
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
    repo = tmp_path / "repo.csv"
    monkeypatch.setattr(em, "REPO_PATH", str(repo))

    def fake_get(url, params=None, **_):
        if "cftc" in url:
            return _Resp(payload=COT_ROWS)
        return _Resp(text=FRED_CSV)

    monkeypatch.setattr(em, "_get_with_retry", fake_get)
    monkeypatch.setattr(em.market_data, "get_spot", lambda: {"price": 4300.0})
    return em.InstitutionalDataIngestor(asset_symbol="XAU/USD")


def test_sdi_is_net_noncommercial_over_open_interest(ingestor):
    cot = ingestor.ingest_institutional_fund_flow()
    expected = (253982 - 28129) / 412800
    assert cot["sdi_raw"] == pytest.approx(expected, abs=1e-6)
    assert cot["net_noncomm"] == 225853.0
    assert cot["report_date"] == "2026-09-22"


def test_dovish_proxy_is_hawkish_when_2y_above_policy(ingestor, monkeypatch):
    # 2y 4.87 vs policy 4.00 -> market prices hikes -> dovish probability < 50.
    monkeypatch.setattr(
        em.InstitutionalDataIngestor, "_fred_latest",
        lambda self, series: ("2026-09-26", 4.0) if series == em.FED_TARGET_SERIES else ("2026-09-24", 4.87),
    )
    macro = ingestor.ingest_weekly_macro_gravity()
    assert macro["fedwatch_dovish_prob"] < 50.0


def test_dovish_proxy_is_dovish_when_2y_below_policy(ingestor, monkeypatch):
    # 2y 3.50 vs policy 4.00 -> market prices cuts -> dovish probability > 50.
    monkeypatch.setattr(
        em.InstitutionalDataIngestor, "_fred_latest",
        lambda self, series: ("2026-09-26", 4.0) if series == em.FED_TARGET_SERIES else ("2026-09-24", 3.5),
    )
    macro = ingestor.ingest_weekly_macro_gravity()
    assert macro["fedwatch_dovish_prob"] > 50.0


@pytest.mark.parametrize(
    "sdi,dovish,expected",
    [(0.6, 0.6, "OPEN"), (0.6, 0.4, "CLOSED"), (0.4, 0.6, "CLOSED"), (0.4, 0.4, "CLOSED")],
)
def test_gate_requires_both_accumulation_and_dovish_tailwind(ingestor, sdi, dovish, expected):
    row = ingestor.synthesize_sentiment_divergence(
        {"fedwatch_dovish_prob": dovish * 100.0}, {"sdi_raw": sdi, "report_date": "2026-09-22"}
    )
    assert row["MACRO_GATE"] == expected


def test_append_is_idempotent_and_sorted(ingestor):
    for _ in range(3):
        ingestor.synthesize_sentiment_divergence(
            {"fedwatch_dovish_prob": 60.0}, {"sdi_raw": 0.6, "report_date": "2026-09-22"}
        )
    ingestor.synthesize_sentiment_divergence(
        {"fedwatch_dovish_prob": 60.0}, {"sdi_raw": 0.6, "report_date": "2026-09-29"}
    )

    df = pd.read_csv(em.REPO_PATH)
    assert len(df) == 2  # repeated weeks collapse, distinct weeks kept
    assert df["week_ending_date"].tolist() == ["2026-09-22", "2026-09-29"]  # chronological
    assert set(df["source"]) == {"CFTC_COT"}


def test_ingestion_carries_forward_spatial_levels(ingestor):
    ingestor.synthesize_sentiment_divergence(
        {"fedwatch_dovish_prob": 60.0}, {"sdi_raw": 0.6, "report_date": "2026-09-22"}
    )
    row = ingestor.synthesize_sentiment_divergence(
        {"fedwatch_dovish_prob": 60.0}, {"sdi_raw": 0.6, "report_date": "2026-09-29"}
    )
    # Second write must inherit the first's spot/floor rather than zeroing them.
    assert row["spot_price"] == pytest.approx(4300.0)
    assert row["liquidity_sweep_floor"] == pytest.approx(4278.5)
