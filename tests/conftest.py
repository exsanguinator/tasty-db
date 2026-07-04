from __future__ import annotations

import itertools
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tastydb.db import init_db
from tastydb.ingest import ingest_payloads
from tastydb.instruments import MetaProvider
from tastydb.matching import rebuild_lots

_ids = itertools.count(1000)


def make_txn(
    *,
    txn_type="Trade",
    sub_type=None,
    action=None,
    symbol="AAPL",
    underlying=None,
    instrument_type="Equity",
    quantity=100,
    price=None,
    value=0.0,
    value_effect="None",
    executed_at="2024-01-02T15:00:00+00:00",
    commission=0.0,
    clearing_fees=0.0,
    regulatory_fees=0.0,
    txn_id=None,
    **extra,
) -> dict:
    """Build a raw API payload shaped like a real /transactions item."""
    executed = datetime.fromisoformat(executed_at)
    payload = {
        "id": txn_id if txn_id is not None else next(_ids),
        "account-number": "5WT00001",
        "transaction-type": txn_type,
        "transaction-sub-type": sub_type or action,
        "action": action,
        "symbol": symbol,
        "underlying-symbol": underlying or (symbol.split()[0] if symbol else None),
        "instrument-type": instrument_type,
        "quantity": quantity,
        "price": price,
        "value": value,
        "value-effect": value_effect,
        "net-value": value,
        "net-value-effect": value_effect,
        "commission": commission,
        "commission-effect": "Debit" if commission else "None",
        "clearing-fees": clearing_fees,
        "clearing-fees-effect": "Debit" if clearing_fees else "None",
        "regulatory-fees": regulatory_fees,
        "regulatory-fees-effect": "Debit" if regulatory_fees else "None",
        "executed-at": executed_at,
        "transaction-date": executed.date().isoformat(),
    }
    payload.update(extra)
    return payload


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    init_db(engine)
    with Session(engine) as s:
        yield s


def run_pipeline(session, payloads, method="fifo", grace_days=4):
    """Ingest payloads then rebuild lots offline (fallback metadata)."""
    ingest_payloads(session, payloads)
    session.commit()
    return rebuild_lots(session, MetaProvider(session, client=None), method=method, grace_days=grace_days)
