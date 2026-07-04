from decimal import Decimal

from sqlalchemy import select

from tastydb.ingest import ingest_payloads
from tastydb.models import ProcessingStatus, RawTransaction

from .conftest import make_txn


def test_ingest_is_idempotent(session):
    payloads = [
        make_txn(txn_id=1, action="Buy to Open", price=10.0, commission=1.0),
        make_txn(txn_id=2, action="Sell to Close", price=12.0),
    ]
    fetched, inserted, updated = ingest_payloads(session, payloads)
    assert (fetched, inserted, updated) == (2, 2, 0)
    session.commit()

    fetched, inserted, updated = ingest_payloads(session, payloads)
    assert (fetched, inserted, updated) == (2, 0, 0)
    assert session.execute(select(RawTransaction)).scalars().all().__len__() == 2


def test_ingest_updates_changed_payload(session):
    ingest_payloads(session, [make_txn(txn_id=1, action="Buy to Open", commission=0.0)])
    session.commit()
    txn = session.get(RawTransaction, 1)
    txn.processing_status = ProcessingStatus.processed
    session.commit()

    # fee reconciled overnight -> payload differs -> row updated, re-flagged
    _, inserted, updated = ingest_payloads(
        session, [make_txn(txn_id=1, action="Buy to Open", commission=1.25)]
    )
    assert (inserted, updated) == (0, 1)
    txn = session.get(RawTransaction, 1)
    assert txn.total_fees == Decimal("1.25")
    assert txn.processing_status == ProcessingStatus.pending


def test_fee_sign_convention(session):
    ingest_payloads(
        session,
        [
            make_txn(
                txn_id=1,
                action="Buy to Open",
                commission=1.0,
                clearing_fees=0.1,
                regulatory_fees=0.05,
            )
        ],
    )
    txn = session.get(RawTransaction, 1)
    assert txn.total_fees == Decimal("1.15")
