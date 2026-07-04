from decimal import Decimal

from sqlalchemy import select

from tastydb.ingest import upsert_accounts
from tastydb.models import Account, Lot, LotClose

from .conftest import make_txn, run_pipeline


def test_lot_ids_are_stable_across_rebuilds(session):
    payloads = [
        make_txn(txn_id=501, action="Buy to Open", quantity=100, price=10.0,
                 executed_at="2024-01-02T15:00:00+00:00"),
        make_txn(txn_id=502, action="Buy to Open", quantity=50, price=11.0,
                 executed_at="2024-01-03T15:00:00+00:00"),
        make_txn(txn_id=503, action="Sell to Close", quantity=120, price=12.0,
                 executed_at="2024-02-01T15:00:00+00:00"),
    ]
    run_pipeline(session, payloads)

    def snapshot():
        lots = {l.lot_id: l.symbol for l in session.execute(select(Lot)).scalars()}
        closes = {
            (c.lot_id, c.broker_close_txn_id): str(c.realized_pnl)
            for c in session.execute(select(LotClose)).scalars()
        }
        return lots, closes

    first_lots, first_closes = snapshot()
    # lot identity IS the opening broker transaction id
    assert set(first_lots) == {501, 502}

    run_pipeline(session, [])  # reprocess with no new data
    second_lots, second_closes = snapshot()
    assert first_lots == second_lots
    assert first_closes == second_closes
    assert set(second_closes) == {(501, 503), (502, 503)}


def test_fee_allocation_is_quantized(session):
    # 1.00 of fees split across a 3-lot close would repeat forever unquantized
    run_pipeline(
        session,
        [
            make_txn(action="Buy to Open", quantity=1, price=10.0,
                     executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Buy to Open", quantity=1, price=10.0,
                     executed_at="2024-01-03T15:00:00+00:00"),
            make_txn(action="Buy to Open", quantity=1, price=10.0,
                     executed_at="2024-01-04T15:00:00+00:00"),
            make_txn(action="Sell to Close", quantity=3, price=12.0, commission=1.0,
                     executed_at="2024-02-01T15:00:00+00:00"),
        ],
    )
    closes = session.execute(select(LotClose)).scalars().all()
    for close in closes:
        assert close.close_fees == Decimal("0.3333")
        assert close.realized_pnl == Decimal("1.6667")  # 2.00 - 0.3333


def test_upsert_accounts_caches_and_updates(session):
    upsert_accounts(session, [
        {"account-number": "5WT00001", "nickname": "Roth IRA",
         "account-type-name": "Roth IRA", "margin-or-cash": "Cash"},
    ])
    session.commit()
    row = session.get(Account, "5WT00001")
    assert row.nickname == "Roth IRA"

    upsert_accounts(session, [
        {"account-number": "5WT00001", "nickname": "Renamed"},
    ])
    session.commit()
    assert session.get(Account, "5WT00001").nickname == "Renamed"
    assert session.execute(select(Account)).scalars().all().__len__() == 1
