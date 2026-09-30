from datetime import date
from decimal import Decimal

from tastydb.analytics import credits_collected, credits_timeseries, list_credit_transactions
from tastydb.ingest import ingest_payloads

from .conftest import make_txn, run_pipeline


def test_sell_and_buy_net_to_signed_total(session):
    run_pipeline(
        session,
        [
            make_txn(action="Sell to Open", symbol="AAPL", quantity=100, price=10.0,
                      value=1000.0, value_effect="Credit",
                      executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Buy to Close", symbol="AAPL", quantity=100, price=8.0,
                      value=800.0, value_effect="Debit",
                      executed_at="2024-02-01T15:00:00+00:00"),
        ],
    )
    rows = credits_collected(session, start=date(2024, 1, 1), end=date(2024, 12, 31))
    assert len(rows) == 1
    assert rows[0].group == "AAPL"
    assert rows[0].trades == 2
    assert rows[0].credits == 200


def test_plain_futures_buy_sell_included(session):
    run_pipeline(
        session,
        [
            make_txn(action="Sell", symbol="/ESZ4", underlying="/ES",
                      instrument_type="Future", quantity=1, price=4500.0,
                      executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Buy", symbol="/ESZ4", underlying="/ES",
                      instrument_type="Future", quantity=1, price=4496.0, value=200.0,
                      value_effect="Credit", executed_at="2024-01-03T15:00:00+00:00"),
        ],
    )
    (row,) = credits_collected(session, underlying="/ES")
    assert row.trades == 1  # only the closing trade books a result
    assert row.credits == 200  # 4 points x $50


def test_open_future_books_nothing_until_closed(session):
    run_pipeline(session, [
        make_txn(action="Sell", symbol="/MESZ4", underlying="/MES",
                 instrument_type="Future", quantity=2, price=4500.25,
                 executed_at="2024-01-02T15:00:00+00:00"),
    ])
    assert credits_collected(session) == []
    assert credits_timeseries(session) == []


def test_unprocessed_future_not_counted(session):
    """No lot_closes yet (synced but not processed): no known result."""
    ingest_payloads(session, [
        make_txn(action="Buy", symbol="/ESZ4", underlying="/ES",
                 instrument_type="Future", quantity=1, price=4500.0,
                 executed_at="2024-01-02T15:00:00+00:00"),
    ])
    assert credits_collected(session) == []


def test_future_reversal_credits_only_closed_part(session):
    """Sell 2 against 1 long closes the long (+10 pts) and opens a short;
    only the close books, and the new short adds nothing."""
    fut = dict(symbol="/MESZ4", underlying="/MES", instrument_type="Future")
    run_pipeline(session, [
        make_txn(action="Buy", quantity=1, price=4500.0,
                 executed_at="2024-01-02T15:00:00+00:00", **fut),
        make_txn(action="Sell", quantity=2, price=4510.0,
                 executed_at="2024-01-03T15:00:00+00:00", **fut),
    ])
    (row,) = credits_collected(session)
    assert row.trades == 1
    assert row.credits == Decimal("50")  # 10 pts x $5


def test_assignment_delivery_leg_included_removal_leg_excluded(session):
    run_pipeline(
        session,
        [
            # option removal leg: no action, no cash-settled subtype -> excluded
            make_txn(txn_type="Receive Deliver", sub_type="Assignment",
                      symbol="AAPL  240119P00150000", underlying="AAPL",
                      instrument_type="Equity Option", quantity=1, value=0.0,
                      value_effect="None", executed_at="2024-01-19T20:00:00+00:00"),
            # delivery leg: stock bought at strike -> included
            make_txn(txn_type="Receive Deliver", action="Buy to Open",
                      symbol="AAPL", underlying="AAPL", instrument_type="Equity",
                      quantity=100, value=15000.0, value_effect="Debit",
                      executed_at="2024-01-19T20:00:00+00:00"),
        ],
    )
    rows = credits_collected(session, underlying="AAPL")
    assert len(rows) == 1
    assert rows[0].trades == 1
    assert rows[0].credits == -15000


def test_cash_settled_expiration_included_paired_removal_excluded(session):
    run_pipeline(
        session,
        [
            # SPX-style cash settlement: real settlement cash in `value`
            make_txn(txn_type="Receive Deliver", sub_type="Cash Settled Assignment",
                      symbol="SPX   240119C05000000", underlying="SPX",
                      instrument_type="Equity Option", quantity=1, price=5000.0,
                      value=2500.0, value_effect="Debit",
                      executed_at="2024-01-19T20:00:00+00:00"),
            # paired zero-value removal -> excluded
            make_txn(txn_type="Receive Deliver", sub_type="Assignment",
                      symbol="SPX   240119C05000000", underlying="SPX",
                      instrument_type="Equity Option", quantity=1, value=0.0,
                      value_effect="None", executed_at="2024-01-19T20:00:00+00:00"),
        ],
    )
    rows = credits_collected(session, underlying="SPX")
    assert len(rows) == 1
    assert rows[0].trades == 1
    assert rows[0].credits == -2500


def test_reversed_pair_nets_to_zero(session):
    run_pipeline(
        session,
        [
            make_txn(action="Sell to Open", symbol="AAPL", quantity=100, price=10.0,
                      value=1000.0, value_effect="Credit", txn_id=5001,
                      executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Sell to Open", symbol="AAPL", quantity=100, price=10.0,
                      value=1000.0, value_effect="Credit", txn_id=5002,
                      executed_at="2024-01-02T16:00:00+00:00",
                      **{"reverses-id": 5001}),
        ],
    )
    rows = credits_collected(session, underlying="AAPL")
    assert rows == []


def test_per_underlying_grouping_and_total(session):
    run_pipeline(
        session,
        [
            make_txn(action="Sell to Open", symbol="AAPL", quantity=100, price=10.0,
                      value=1000.0, value_effect="Credit",
                      executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Sell to Open", symbol="MSFT", quantity=10, price=100.0,
                      value=1000.0, value_effect="Credit",
                      executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Buy to Close", symbol="MSFT", quantity=10, price=90.0,
                      value=900.0, value_effect="Debit",
                      executed_at="2024-02-01T15:00:00+00:00"),
        ],
    )
    rows = credits_collected(session)
    by_symbol = {r.group: r.credits for r in rows}
    assert by_symbol["AAPL"] == 1000
    assert by_symbol["MSFT"] == 100
    assert sum(r.credits for r in rows) == 1100


def test_credits_timeseries_accumulates_per_day(session):
    run_pipeline(
        session,
        [
            make_txn(action="Sell to Open", symbol="AAPL", quantity=100, price=10.0,
                      value=1000.0, value_effect="Credit",
                      executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Sell to Open", symbol="MSFT", quantity=100, price=5.0,
                      value=500.0, value_effect="Credit",
                      executed_at="2024-01-02T16:00:00+00:00"),
            make_txn(action="Buy to Close", symbol="AAPL", quantity=100, price=8.0,
                      value=800.0, value_effect="Debit",
                      executed_at="2024-02-01T15:00:00+00:00"),
        ],
    )
    points = credits_timeseries(session)
    assert points == [
        (date(2024, 1, 2), 1500, 1500),
        (date(2024, 2, 1), -800, 700),
    ]
    total = sum(r.credits for r in credits_collected(session))
    assert points[-1][2] == total
    assert credits_timeseries(session, underlying="MSFT") == [(date(2024, 1, 2), 500, 500)]


def _nnq_trail():
    """Real /NNQZ6 ($0.20/pt) trail, 2026-09: three shorts, bought back. Each
    trade's `value` is only the cash since the prior daily settlement (sells
    are 0); the rest arrives in Money Movement / Mark to Market rows."""
    fut = dict(symbol="/NNQZ6", underlying="/NNQ", instrument_type="Future")

    def mtm(qty, price, value, effect, at):
        return make_txn(txn_type="Money Movement", sub_type="Mark to Market",
                        quantity=qty, price=price, value=value, value_effect=effect,
                        executed_at=at, **fut)

    return [
        make_txn(action="Sell", quantity=1, price=29751.5,
                 executed_at="2026-09-17T17:02:10+00:00", **fut),
        mtm(1, 29743.0, 1.7, "Credit", "2026-09-17T21:00:00+00:00"),
        make_txn(action="Buy", quantity=1, price=29727.5, value=3.1, value_effect="Credit",
                 executed_at="2026-09-18T16:55:11+00:00", **fut),
        make_txn(action="Sell", quantity=1, price=30241.0,
                 executed_at="2026-09-21T10:14:26+00:00", **fut),
        mtm(1, 30785.0, 108.8, "Debit", "2026-09-21T21:00:00+00:00"),
        make_txn(action="Sell", quantity=1, price=30843.0,
                 executed_at="2026-09-21T23:24:35+00:00", **fut),
        mtm(2, 31028.5, 85.8, "Debit", "2026-09-22T21:00:00+00:00"),
        mtm(2, 30765.0, 105.4, "Credit", "2026-09-23T21:00:00+00:00"),
        make_txn(action="Buy", quantity=2, price=30585.0, value=72.0, value_effect="Credit",
                 executed_at="2026-09-24T07:12:01+00:00", **fut),
    ]


def test_futures_mark_to_market_rows_excluded(session):
    """Futures book their gross PnL only when closed; opens and the daily MTM
    rows add nothing. Gross realized = (24 + 518 - 604) pts x 0.2."""
    run_pipeline(session, _nnq_trail())
    (row,) = credits_collected(session, underlying="/NNQ")
    assert row.trades == 2  # the two buy-to-close trades
    assert row.credits == Decimal("-12.40")
    assert credits_timeseries(session, underlying="/NNQ") == [
        (date(2026, 9, 18), Decimal("4.80"), Decimal("4.80")),
        (date(2026, 9, 24), Decimal("-17.20"), Decimal("-12.40")),
    ]


def test_futures_credits_attributed_to_lot_strategy(session):
    run_pipeline(session, _nnq_trail())
    rows = credits_collected(session, group_by="strategy")
    assert [(r.group, r.credits) for r in rows] == [("Short future", Decimal("-12.40"))]
    txns, count, total = list_credit_transactions(session, strategy="Short future")
    assert count == 2 and total == Decimal("-12.40")
    assert all(t.action == "Buy" for t in txns)
