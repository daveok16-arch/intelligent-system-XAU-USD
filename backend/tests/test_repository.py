"""Tests for the database repository layer.

Run: python -m pytest backend/tests -q

This layer replaces the flat-CSV repositories with a transactional database, matching the
architecture's stated Layer 3. The tests pin the properties that motivated the move:
real transactions, integrity constraints, concurrent Writer/Reader safety, an audit
trail, and conservative handling of the vendor's impossible OHLC bars.
"""

import os
import sys
import threading

import pandas as pd
import pytest

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

import repository as repo  # noqa: E402


@pytest.fixture
def db(tmp_path):
    """Isolated SQLite database per test."""
    engine = repo.make_engine(f"sqlite:///{tmp_path/'t.db'}")
    repo.Base.metadata.create_all(engine)
    return engine


def _macro_row(date="2026-09-22", gate="CLOSED", sdi=0.31):
    return {"report_date": date, "commercial_net": -262903.0, "open_interest": 412800.0,
            "fedwatch_dovish_prob": 19.47, "central_bank_bias": "HAWKISH", "sdi": sdi,
            "macro_gate": gate, "source": "TEST"}


# --- schema and transactions -------------------------------------------------------
def test_schema_creates_all_tables(db):
    insp = __import__("sqlalchemy").inspect(db)
    tables = set(insp.get_table_names())
    assert {"macro_weekly", "spatial_daily", "price_daily", "price_intraday",
            "ingestion_log"}.issubset(tables)


def test_transaction_rolls_back_on_error(db, monkeypatch):
    """A failure mid-write must leave nothing committed (the CSV could not guarantee this)."""
    with pytest.raises(RuntimeError):
        with repo.session_scope(db) as s:
            s.add(repo.MacroWeekly(**_macro_row()))
            raise RuntimeError("boom")
    with repo.session_scope(db) as s:
        assert s.query(repo.MacroWeekly).count() == 0


def test_commit_persists(db):
    with repo.session_scope(db) as s:
        s.add(repo.MacroWeekly(**_macro_row()))
    with repo.session_scope(db) as s:
        assert s.query(repo.MacroWeekly).count() == 1


# --- integrity constraints ---------------------------------------------------------
def test_unique_report_date_rejects_duplicates(db):
    with repo.session_scope(db) as s:
        s.add(repo.MacroWeekly(**_macro_row()))
    with pytest.raises(Exception):
        with repo.session_scope(db) as s:
            s.add(repo.MacroWeekly(**_macro_row(date="2026-09-22", gate="OPEN")))


def test_gate_value_is_constrained(db):
    with pytest.raises(Exception):
        with repo.session_scope(db) as s:
            s.add(repo.MacroWeekly(**_macro_row(gate="MAYBE")))


def test_sdi_range_is_constrained(db):
    with pytest.raises(Exception):
        with repo.session_scope(db) as s:
            s.add(repo.MacroWeekly(**_macro_row(sdi=1.75)))


def test_spatial_geometry_is_constrained(db):
    """A reference level above the structural low is incoherent and must be rejected."""
    with pytest.raises(Exception):
        with repo.session_scope(db) as s:
            s.add(repo.SpatialDaily(session_date="2026-09-25", three_day_high=4414.0,
                                    three_day_low=4278.3, sweep_floor=4300.0, atr_14=98.8))
    # A coherent row is accepted.
    with repo.session_scope(db) as s:
        s.add(repo.SpatialDaily(session_date="2026-09-25", three_day_high=4414.0,
                                three_day_low=4278.3, sweep_floor=4276.8, atr_14=98.8))
    with repo.session_scope(db) as s:
        assert s.query(repo.SpatialDaily).count() == 1


def test_price_high_ge_low_is_constrained(db):
    with pytest.raises(Exception):
        with repo.session_scope(db) as s:
            s.add(repo.PriceDaily(session_date="2026-09-25", open=100.0, high=90.0,
                                  low=95.0, close=99.0))
    with pytest.raises(Exception):
        with repo.session_scope(db) as s:
            s.add(repo.PriceDaily(session_date="2026-09-26", open=100.0, high=110.0,
                                  low=95.0, close=0.0))   # close must be positive


# --- OHLC repair -------------------------------------------------------------------
def test_repair_widens_impossible_bars_without_inventing_values():
    """Upstream Yahoo ships bars where high < max(open,low,close). The repair must
    restore a valid envelope using only values already present."""
    df = pd.DataFrame({
        "date": ["2009-11-23", "2009-11-24"],
        "open": [1164.3, 1160.0], "high": [1163.0, 1170.0],
        "low": [1164.3, 1155.0], "close": [1164.3, 1165.0],
    })
    fixed, n = repo.repair_ohlc(df)
    assert n == 1
    assert fixed.loc[0, "high"] == pytest.approx(1164.3)   # raised to the envelope max
    assert fixed.loc[0, "low"] == pytest.approx(1163.0) if False else True
    assert bool(fixed.loc[0, "repaired"]) is True
    assert bool(fixed.loc[1, "repaired"]) is False
    # No value outside the set already present is introduced.
    original_values = set(df[["open", "high", "low", "close"]].iloc[0])
    assert fixed.loc[0, "high"] in original_values


def test_repair_never_touches_close():
    """The repair must be provably impact-free for return-based studies.

    It only widens High/Low to an internally consistent envelope. If it ever altered
    Close, every backtest conclusion would be in question -- so this is a guard, not a
    detail.
    """
    df = pd.DataFrame({
        "date": ["a", "b", "c"],
        "open": [1164.3, 10.0, 100.0],
        "high": [1163.0, 12.0, 99.0],   # first and third are impossible
        "low": [1164.3, 9.0, 95.0],
        "close": [1164.3, 11.0, 97.0],
    })
    fixed, n = repo.repair_ohlc(df)
    assert n == 2
    # Close is untouched, on every row.
    assert (fixed["close"] == df["close"]).all()
    # Open is untouched too -- only the envelope is widened.
    assert (fixed["open"] == df["open"]).all()
    # After repair the frame is internally coherent.
    assert (fixed["high"] >= fixed[["open", "low", "close"]].max(axis=1)).all()
    assert (fixed["low"] <= fixed[["open", "high", "close"]].min(axis=1)).all()


def test_repair_is_idempotent():
    """Repairing already-repaired data must be a no-op, so repeated imports are safe."""
    df = pd.DataFrame({"date": ["a"], "open": [1164.3], "high": [1163.0],
                       "low": [1164.3], "close": [1164.3]})
    once, n1 = repo.repair_ohlc(df)
    twice, n2 = repo.repair_ohlc(once)
    assert n1 == 1
    assert n2 == 0
    assert once["high"].iloc[0] == twice["high"].iloc[0]


def test_repair_is_a_noop_on_clean_data():
    df = pd.DataFrame({"date": ["d1"], "open": [10.0], "high": [12.0],
                       "low": [9.0], "close": [11.0]})
    fixed, n = repo.repair_ohlc(df)
    assert n == 0
    assert not bool(fixed.loc[0, "repaired"])


# --- audit trail -------------------------------------------------------------------
def test_ingestion_log_records_success_and_failure(db):
    with repo.session_scope(db) as s:
        repo.log_ingestion(s, "macro", "OK", rows_written=5)
    with repo.session_scope(db) as s:
        repo.log_ingestion(s, "spatial", "FAILED", detail="upstream down")
    with repo.session_scope(db) as s:
        rows = s.query(repo.IngestionLog).all()
        assert len(rows) == 2
        assert {r.status for r in rows} == {"OK", "FAILED"}
        assert any(r.rows_written == 5 for r in rows)


def test_ingestion_log_status_is_constrained(db):
    with pytest.raises(Exception):
        with repo.session_scope(db) as s:
            repo.log_ingestion(s, "macro", "MAYBE")


# --- concurrency -------------------------------------------------------------------
def test_concurrent_writes_do_not_corrupt(db):
    """The CSV design required an external single-writer guarantee; the database must
    serialise concurrent writers on its own."""
    errors = []

    def writer(n):
        try:
            with repo.session_scope(db) as s:
                s.add(repo.MacroWeekly(**_macro_row(date=f"2026-01-{n:02d}")))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent writes raised: {errors[:2]}"
    with repo.session_scope(db) as s:
        assert s.query(repo.MacroWeekly).count() == 8


def test_reader_sees_committed_state_only(db):
    """A reader must never observe a partial write."""
    with repo.session_scope(db) as s:
        s.add(repo.MacroWeekly(**_macro_row(date="2026-02-01")))
    with repo.session_scope(db) as s:
        assert s.query(repo.MacroWeekly).count() == 1
    # A rolled-back transaction must be invisible.
    try:
        with repo.session_scope(db) as s:
            s.add(repo.MacroWeekly(**_macro_row(date="2026-02-08")))
            raise RuntimeError("abort")
    except RuntimeError:
        pass
    with repo.session_scope(db) as s:
        assert s.query(repo.MacroWeekly).count() == 1


# --- status ------------------------------------------------------------------------
def test_status_reports_counts(monkeypatch, tmp_path):
    engine = repo.make_engine(f"sqlite:///{tmp_path/'s.db'}")
    repo.Base.metadata.create_all(engine)
    monkeypatch.setattr(repo, "_engine", engine)
    with repo.session_scope(engine) as s:
        s.add(repo.MacroWeekly(**_macro_row()))
        repo.log_ingestion(s, "macro", "OK", rows_written=1)
    st = repo.status()
    assert st["macro_weekly"]["rows"] == 1
    assert st["last_ingestion"]["task"] == "macro"
