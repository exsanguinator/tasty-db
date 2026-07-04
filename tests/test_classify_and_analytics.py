from datetime import date

from sqlalchemy import select

from tastydb.analytics import realized_pnl
from tastydb.models import ProcessingStatus, RawTransaction

from .conftest import make_txn, run_pipeline


def test_non_position_transactions_are_ignored_not_dropped_silently(session):
    run_pipeline(
        session,
        [
            make_txn(txn_type="Money Movement", sub_type="Deposit", symbol=None,
                     instrument_type=None, quantity=0),
            make_txn(txn_type="Receive Deliver", sub_type="Forward Split",
                     symbol="AAPL", instrument_type="Equity", quantity=100),
        ],
    )
    statuses = {
        t.transaction_sub_type: t.processing_status
        for t in session.execute(select(RawTransaction)).scalars()
    }
    assert statuses["Deposit"] == ProcessingStatus.ignored
    assert statuses["Forward Split"] == ProcessingStatus.unsupported  # flagged for review


def test_unknown_receive_deliver_subtype_is_flagged(session):
    run_pipeline(
        session,
        [
            make_txn(txn_type="Receive Deliver", sub_type="Some Future Thing",
                     symbol="AAPL", instrument_type="Equity", quantity=100),
        ],
    )
    txn = session.execute(select(RawTransaction)).scalar_one()
    assert txn.processing_status == ProcessingStatus.unsupported
    assert "Some Future Thing" in txn.processing_note


def test_pnl_report_date_range_and_grouping(session):
    run_pipeline(
        session,
        [
            make_txn(action="Buy to Open", symbol="AAPL", quantity=100, price=10.0,
                     executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Sell to Close", symbol="AAPL", quantity=100, price=12.0,
                     commission=1.0, executed_at="2024-02-01T15:00:00+00:00"),
            make_txn(action="Buy to Open", symbol="MSFT", quantity=10, price=100.0,
                     executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Sell to Close", symbol="MSFT", quantity=10, price=90.0,
                     executed_at="2024-03-01T15:00:00+00:00"),
        ],
    )
    rows = realized_pnl(session, start=date(2024, 1, 1), end=date(2024, 12, 31))
    by_symbol = {r.group: r for r in rows}
    assert by_symbol["AAPL"].realized_pnl == 199  # (12-10)*100 - 1
    assert by_symbol["MSFT"].realized_pnl == -100

    # narrow range excludes the March close (end date is inclusive)
    feb_only = realized_pnl(session, start=date(2024, 2, 1), end=date(2024, 2, 1))
    assert [r.group for r in feb_only] == ["AAPL"]

    # underlying filter
    only_msft = realized_pnl(session, underlying="MSFT")
    assert len(only_msft) == 1 and only_msft[0].realized_pnl == -100

    # asset-type grouping
    by_type = realized_pnl(session, group_by="asset_type")
    assert by_type[0].group == "stock"


def test_pnl_matches_spec_formula(session):
    """(close - open) * qty * multiplier * side_sign - fees, summed from closes."""
    opt = "SPY   240119P00450000"
    run_pipeline(
        session,
        [
            make_txn(action="Sell to Open", symbol=opt, underlying="SPY",
                     instrument_type="Equity Option", quantity=2, price=1.50,
                     commission=2.0, executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Buy to Close", symbol=opt, underlying="SPY",
                     instrument_type="Equity Option", quantity=2, price=0.50,
                     commission=2.0, executed_at="2024-01-10T15:00:00+00:00"),
        ],
    )
    rows = realized_pnl(session, underlying="SPY")
    # (1.50 - 0.50) * 2 * 100 - 4.00 total fees
    assert rows[0].realized_pnl == 196
    assert rows[0].fees == 4
