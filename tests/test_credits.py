from datetime import date

from tastydb.analytics import credits_collected

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
                      instrument_type="Future", quantity=1, value=500.0,
                      value_effect="Credit", executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Buy", symbol="/ESZ4", underlying="/ES",
                      instrument_type="Future", quantity=1, value=300.0,
                      value_effect="Debit", executed_at="2024-01-03T15:00:00+00:00"),
        ],
    )
    rows = credits_collected(session, underlying="/ES")
    assert len(rows) == 1
    assert rows[0].credits == 200


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
