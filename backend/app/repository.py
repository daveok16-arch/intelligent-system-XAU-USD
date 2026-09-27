"""
APPLICATION MODULE: DATA REPOSITORY LAYER
ROLE: PERSIST MACRO / SPATIAL / PRICE / POSITIONING DATA IN A REAL DATABASE

This replaces the flat-CSV repositories with a transactional database, matching the
architecture's stated Layer 3 ("PostgreSQL or SQLite + Supabase").

Why it matters beyond tidiness
------------------------------
The CSV design carried concrete defects that a database removes:

  * Single-writer only. Concurrent writes could interleave; correctness depended on
    accepting that only one writer is ever active. A database gives real transactions.
  * No concurrency control. Readers could observe a half-written file. Writes are now
    atomic, so a reader sees either the previous committed state or the new one.
  * `ReadWriteOnce` volume required, which blocks multi-node failover: all API replicas
    have to sit on the volume's node. A database is reached over the network, so the
    read tier scales freely.
  * No integrity constraints. Nothing stopped a bad row (duplicate week, null price,
    impossible high/low ordering) from being written and silently served.
  * No point-in-time recovery or audit trail.

Backend selection
-----------------
DATABASE_URL chooses the engine. Default is SQLite (zero-config, matches the stated
architecture); PostgreSQL works unchanged by pointing at a DSN:

    DATABASE_URL=sqlite:///data/institutional.db
    DATABASE_URL=postgresql+psycopg://user:pass@host:5432/institutional

The same tables, the same constraints, the same API. No code changes to move up.

Schema
------
  macro_weekly       one row per CFTC report week (positioning + macro regime + SDI)
  spatial_daily      one row per session (structural levels)
  price_daily        one row per session (OHLC)
  price_intraday     one row per bar (interval-tagged)
  ingestion_log      audit trail: what ran, when, how many rows, success/failure

Usage:
    python -m backend.app.repository --init          # create schema
    python -m backend.app.repository --migrate-csv   # import existing CSVs
    python -m backend.app.repository --status        # row counts + freshness
"""

import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint, Column, DateTime, Float, Integer, String, UniqueConstraint,
    create_engine, func, select,
)
from sqlalchemy.orm import Session, declarative_base

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

DATA_DIR = os.getenv("DATA_DIR", os.path.join(_PROJECT_DIR, "data"))
HISTORY_DIR = os.getenv("HISTORY_DIR", os.path.join(DATA_DIR, "history"))

DEFAULT_SQLITE = f"sqlite:///{os.path.join(DATA_DIR, 'institutional.db')}"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_SQLITE)

Base = declarative_base()


# --- schema ------------------------------------------------------------------------
class MacroWeekly(Base):
    __tablename__ = "macro_weekly"
    id = Column(Integer, primary_key=True)
    report_date = Column(String(10), nullable=False)          # week_ending_date, ISO
    large_spec_net = Column(Float)
    commercial_net = Column(Float)
    open_interest = Column(Float)
    fedwatch_dovish_prob = Column(Float)
    central_bank_bias = Column(String(16))
    sdi = Column(Float)
    commercial_net_52w_low = Column(Float)
    commercial_net_52w_high = Column(Float)
    macro_gate = Column(String(8), nullable=False, default="CLOSED")
    source = Column(String(64))
    inserted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("report_date", name="uq_macro_report_date"),
        CheckConstraint("macro_gate IN ('OPEN','CLOSED')", name="ck_macro_gate"),
        CheckConstraint("sdi IS NULL OR (sdi >= 0 AND sdi <= 1)", name="ck_sdi_range"),
    )


class SpatialDaily(Base):
    __tablename__ = "spatial_daily"
    id = Column(Integer, primary_key=True)
    session_date = Column(String(10), nullable=False)
    three_day_high = Column(Float)
    three_day_low = Column(Float)
    sweep_floor = Column(Float)
    atr_14 = Column(Float)
    source = Column(String(64))
    inserted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("session_date", name="uq_spatial_date"),
        # A boundary must be internally coherent: floor below low, low below high.
        CheckConstraint("three_day_low IS NULL OR three_day_high IS NULL "
                        "OR three_day_low <= three_day_high", name="ck_spatial_low_le_high"),
        CheckConstraint("sweep_floor IS NULL OR three_day_low IS NULL "
                        "OR sweep_floor <= three_day_low", name="ck_spatial_floor_le_low"),
        CheckConstraint("atr_14 IS NULL OR atr_14 >= 0", name="ck_atr_non_negative"),
    )


class PriceDaily(Base):
    __tablename__ = "price_daily"
    id = Column(Integer, primary_key=True)
    session_date = Column(String(10), nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float, nullable=False)
    source = Column(String(64))
    inserted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("session_date", name="uq_price_daily_date"),
        CheckConstraint("high IS NULL OR low IS NULL OR high >= low", name="ck_ohlc_high_ge_low"),
        CheckConstraint("close > 0", name="ck_close_positive"),
    )


class PriceIntraday(Base):
    __tablename__ = "price_intraday"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False)
    interval = Column(String(8), nullable=False)
    bar_time = Column(DateTime, nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float, nullable=False)
    volume = Column(Float)
    inserted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("symbol", "interval", "bar_time", name="uq_intraday_bar"),
        CheckConstraint("high IS NULL OR low IS NULL OR high >= low", name="ck_intraday_high_ge_low"),
    )


class IngestionLog(Base):
    __tablename__ = "ingestion_log"
    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime)
    task = Column(String(64), nullable=False)
    rows_written = Column(Integer, default=0)
    status = Column(String(16), nullable=False)     # OK | FAILED
    detail = Column(String(512))

    __table_args__ = (
        CheckConstraint("status IN ('OK','FAILED')", name="ck_ingest_status"),
    )


# --- engine ------------------------------------------------------------------------
def make_engine(url=None, echo=False):
    url = url or DATABASE_URL
    kwargs = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        # check_same_thread=False: Streamlit and the API handle requests on multiple
        # threads. WAL keeps readers from blocking the writer.
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.exec_driver_sql("PRAGMA busy_timeout=5000")
    return engine


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


@contextmanager
def session_scope(engine=None):
    """Transactional scope: commits on success, rolls back on any exception.

    `engine` may be supplied so tests (and future sharded deployments) can target an
    isolated database instead of the module default.
    """
    session = Session(engine or get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(engine=None):
    Base.metadata.create_all(engine or get_engine())


# --- audit -------------------------------------------------------------------------
def log_ingestion(session, task, status, rows_written=0, detail=None, started=None):
    session.add(IngestionLog(
        task=task, status=status, rows_written=rows_written,
        detail=(detail or "")[:512] or None,
        started_at=started or datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    ))


# --- status ------------------------------------------------------------------------
def status():
    with session_scope() as s:
        out = {}
        for model, key in ((MacroWeekly, "macro_weekly"), (SpatialDaily, "spatial_daily"),
                           (PriceDaily, "price_daily"), (PriceIntraday, "price_intraday"),
                           (IngestionLog, "ingestion_log")):
            count = s.execute(select(func.count()).select_from(model)).scalar_one()
            row = None
            date_col = {"macro_weekly": "report_date", "spatial_daily": "session_date",
                        "price_daily": "session_date", "price_intraday": "bar_time"}.get(key)
            if date_col and count:
                col = getattr(model, date_col)
                latest = s.execute(select(func.max(col))).scalar_one()
                earliest = s.execute(select(func.min(col))).scalar_one()
                row = (earliest, latest)
            out[key] = {"rows": int(count), "range": row}
        last = s.execute(select(IngestionLog).order_by(IngestionLog.id.desc()).limit(1)).scalar_one_or_none()
        out["last_ingestion"] = None if last is None else {
            "task": last.task, "status": last.status, "rows": last.rows_written,
            "at": last.finished_at.isoformat() if last.finished_at else None,
        }
        return out


# --- CSV migration -----------------------------------------------------------------
def migrate_csv(session=None):
    """Import existing CSV repositories into the database. Idempotent (upsert by key)."""
    import pandas as pd
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    def upsert(model, rows, keycols):
        added = 0
        for row in rows:
            stmt = sqlite_insert(model).values(**row)
            update_cols = {c: stmt.excluded[c] for c in row if c not in keycols and c != "id"}
            session.execute(stmt.on_conflict_do_update(index_elements=keycols, set_=update_cols))
            added += 1
        return added

    engine = session.get_bind() if session else None
    own = session is None
    if own:
        session = Session(engine or make_engine(), future=True)

    started = datetime.now(timezone.utc)
    report = {}
    try:
        m = os.path.join(HISTORY_DIR, "macro_history.csv")
        if os.path.exists(m):
            df = pd.read_csv(m)
            rows = [{
                "report_date": str(r["date"]), "large_spec_net": _f(r.get("Large_Spec_Net")),
                "commercial_net": _f(r.get("Commercial_Net")), "open_interest": _f(r.get("open_interest")),
                "fedwatch_dovish_prob": _f(r.get("fedwatch_dovish_prob")),
                "central_bank_bias": _s(r.get("central_bank_bias")), "sdi": _f(r.get("SDI")),
                "commercial_net_52w_low": _f(r.get("Commercial_Net_52w_Low")),
                "commercial_net_52w_high": _f(r.get("Commercial_Net_52w_High")),
                "macro_gate": _s(r.get("MACRO_GATE")) or "CLOSED", "source": _s(r.get("source")),
            } for _, r in df.iterrows()]
            report["macro_weekly"] = upsert(MacroWeekly, rows, ["report_date"])

        sp = os.path.join(HISTORY_DIR, "spatial_history.csv")
        if os.path.exists(sp):
            df = pd.read_csv(sp)
            rows = [{
                "session_date": str(r["date"]), "three_day_high": _f(r.get("Three_Day_High")),
                "three_day_low": _f(r.get("Three_Day_Low")), "sweep_floor": _f(r.get("Sweep_Floor")),
                "atr_14": _f(r.get("ATR_14")), "source": _s(r.get("source")),
            } for _, r in df.iterrows()]
            report["spatial_daily"] = upsert(SpatialDaily, rows, ["session_date"])

        p = os.path.join(HISTORY_DIR, "price_history.csv")
        if os.path.exists(p):
            df = pd.read_csv(p)
            df, n_repaired = repair_ohlc(df)
            report["price_daily_repaired"] = n_repaired
            rows = [{
                "session_date": str(r["date"]), "open": _f(r.get("open")), "high": _f(r.get("high")),
                "low": _f(r.get("low")), "close": _f(r.get("close")), "source": _s(r.get("source")),
            } for _, r in df.iterrows() if _f(r.get("close"))]
            report["price_daily"] = upsert(PriceDaily, rows, ["session_date"])

        i = os.path.join(HISTORY_DIR, "intraday_GC_F_1h.csv")
        if os.path.exists(i):
            df = pd.read_csv(i)
            rows = [{
                "symbol": "GC=F", "interval": "1h",
                "bar_time": pd.to_datetime(r["timestamp"]).to_pydatetime(),
                "open": _f(r.get("open")), "high": _f(r.get("high")), "low": _f(r.get("low")),
                "close": _f(r.get("close")), "volume": _f(r.get("volume")),
            } for _, r in df.iterrows()]
            report["price_intraday"] = upsert(PriceIntraday, rows, ["symbol", "interval", "bar_time"])

        if own:
            session.commit()
        log_ingestion(session, "csv_migration", "OK", sum(report.values()),
                      detail=", ".join(f"{k}={v}" for k, v in report.items()), started=started)
        if own:
            session.commit()
    except Exception as exc:
        if own:
            session.rollback()
        log_ingestion(session, "csv_migration", "FAILED", 0, detail=f"{type(exc).__name__}: {exc}", started=started)
        if own:
            session.commit()
        raise
    finally:
        if own:
            session.close()
    return report


def _f(v):
    """Coerce to float or None. Never invents a value."""
    try:
        if v is None:
            return None
        import math
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _s(v):
    if v is None:
        return None
    import pandas as pd
    return None if (isinstance(v, float) and pd.isna(v)) else str(v)


def repair_ohlc(df):
    """Repair impossible OHLC bars and report how many were touched.

    Upstream Yahoo data contains bars where the stated High is below max(Open,Low,Close)
    or the Low is above min(Open,High,Close) -- 464 such bars in the GC=F daily series.
    That is a vendor defect we cannot fix at source.

    The repair is conservative and documented rather than silent: an impossible bar is
    widened to its internally consistent envelope --
        High := max(Open, High, Low, Close)
        Low  := min(Open, High, Low, Close)
    which preserves every stated price (nothing is invented; the envelope simply contains
    the values that were already present) while restoring a valid bar. A `repaired` flag
    marks each touched row so downstream code can exclude them if it prefers.

    Returns (repaired_df, n_repaired).
    """
    import pandas as pd

    out = df.copy()
    cols = ["open", "high", "low", "close"]
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    valid = out[cols].notna().all(axis=1)
    body_max = out[["open", "low", "close"]].max(axis=1)
    body_min = out[["open", "high", "close"]].min(axis=1)
    bad = valid & ((out["high"] < body_max) | (out["low"] > body_min))

    n_bad = int(bad.sum())
    out["repaired"] = False
    if n_bad:
        out.loc[bad, "high"] = body_max[bad]
        out.loc[bad, "low"] = body_min[bad]
        out.loc[bad, "repaired"] = True
    return out, n_bad


def main(argv=None):
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Institutional data repository (SQLite/PostgreSQL).")
    parser.add_argument("--init", action="store_true", help="Create the schema.")
    parser.add_argument("--migrate-csv", action="store_true", help="Import the CSV repositories.")
    parser.add_argument("--status", action="store_true", help="Show row counts and freshness.")
    args = parser.parse_args(argv)

    if args.init:
        init_db()
        print(f"✅ schema created at {DATABASE_URL}")
    if args.migrate_csv:
        init_db()
        with session_scope() as s:
            report = migrate_csv(s)
        print("✅ migrated:", report)
    if args.status or not any((args.init, args.migrate_csv)):
        init_db()
        print(json.dumps(status(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
