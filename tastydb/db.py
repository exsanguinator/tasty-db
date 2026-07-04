"""Engine/session helpers."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def make_engine(db_url: str):
    return create_engine(db_url)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def _drop_legacy_derived_tables(engine) -> None:
    """Databases created before the lots-table redesign have `open_lots` (and
    an old-shape `lot_closes`). Both are derived tables rebuilt from raw on
    every `process`, so dropping them is lossless."""
    if "open_lots" in inspect(engine).get_table_names():
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS lot_closes"))
            conn.execute(text("DROP TABLE IF EXISTS open_lots"))


def init_db(engine) -> None:
    _drop_legacy_derived_tables(engine)
    Base.metadata.create_all(engine)
