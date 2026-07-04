"""Engine/session helpers."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def make_engine(db_url: str):
    return create_engine(db_url)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine) -> None:
    Base.metadata.create_all(engine)
