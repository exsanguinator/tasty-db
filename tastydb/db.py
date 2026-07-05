"""Engine/session helpers."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def make_engine(db_url: str):
    return create_engine(db_url)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


# Tables that are pure derivations of raw_transactions: dropping them is
# lossless (rebuilt by `tastydb process`), which is how schema changes to them
# are "migrated" — create_all only ever adds tables, never columns.
_DERIVED_TABLES = ("lot_closes", "lots")


def _ensure_derived_schema(engine) -> None:
    """Drop the derived tables when their columns no longer match the models
    (or when the pre-rename `open_lots` table exists), so create_all can
    recreate them at the current schema."""
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    stale = "open_lots" in existing
    for name in _DERIVED_TABLES:
        if stale or name not in existing:
            continue
        actual = {col["name"] for col in inspector.get_columns(name)}
        expected = {col.name for col in Base.metadata.tables[name].columns}
        if actual != expected:
            stale = True
    if stale:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS open_lots"))
            for name in _DERIVED_TABLES:  # closes first (FK on lots)
                conn.execute(text(f"DROP TABLE IF EXISTS {name}"))


def init_db(engine) -> None:
    _ensure_derived_schema(engine)
    Base.metadata.create_all(engine)
