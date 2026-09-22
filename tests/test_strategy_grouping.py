"""Strategy names stamped at rebuild time, and reports grouped by them."""

from decimal import Decimal

from sqlalchemy import select

from tastydb.analytics import (
    credits_collected,
    list_credit_transactions,
    realized_pnl,
    strategies,
)
from tastydb.models import Lot, LotClose

from .conftest import make_txn, run_pipeline


def _vertical_and_strangle():
    """Two option trades, both opened and closed:

    order 7001 — a SPY put credit spread (short 400 / long 390), closed by 7002.
    order 7011 — an XSP short strangle (short 560 put / short 600 call),
                 closed by 7012.
    """
    return [
        # put credit spread: sell the 400, buy the 390
        make_txn(txn_id=201, action="Sell to Open", symbol="SPY   260320P00400000",
                 instrument_type="Equity Option", quantity=1, price=5.00,
                 value=500.0, value_effect="Credit",
                 executed_at="2026-01-05T15:00:00+00:00", **{"order-id": 7001}),
        make_txn(txn_id=202, action="Buy to Open", symbol="SPY   260320P00390000",
                 instrument_type="Equity Option", quantity=1, price=3.00,
                 value=300.0, value_effect="Debit",
                 executed_at="2026-01-05T15:00:00+00:00", **{"order-id": 7001}),
        make_txn(txn_id=203, action="Buy to Close", symbol="SPY   260320P00400000",
                 instrument_type="Equity Option", quantity=1, price=1.00,
                 value=100.0, value_effect="Debit",
                 executed_at="2026-02-05T15:00:00+00:00", **{"order-id": 7002}),
        make_txn(txn_id=204, action="Sell to Close", symbol="SPY   260320P00390000",
                 instrument_type="Equity Option", quantity=1, price=0.50,
                 value=50.0, value_effect="Credit",
                 executed_at="2026-02-05T15:00:00+00:00", **{"order-id": 7002}),
        # short strangle
        make_txn(txn_id=211, action="Sell to Open", symbol="XSP   260320P00560000",
                 instrument_type="Equity Option", quantity=1, price=4.00,
                 value=400.0, value_effect="Credit",
                 executed_at="2026-01-06T15:00:00+00:00", **{"order-id": 7011}),
        make_txn(txn_id=212, action="Sell to Open", symbol="XSP   260320C00600000",
                 instrument_type="Equity Option", quantity=1, price=2.00,
                 value=200.0, value_effect="Credit",
                 executed_at="2026-01-06T15:00:00+00:00", **{"order-id": 7011}),
        make_txn(txn_id=213, action="Buy to Close", symbol="XSP   260320P00560000",
                 instrument_type="Equity Option", quantity=1, price=1.00,
                 value=100.0, value_effect="Debit",
                 executed_at="2026-02-06T15:00:00+00:00", **{"order-id": 7012}),
        make_txn(txn_id=214, action="Buy to Close", symbol="XSP   260320C00600000",
                 instrument_type="Equity Option", quantity=1, price=0.25,
                 value=25.0, value_effect="Debit",
                 executed_at="2026-02-06T15:00:00+00:00", **{"order-id": 7012}),
    ]


def test_lots_and_closes_are_stamped(session):
    run_pipeline(session, _vertical_and_strangle())
    lots = {l.lot_id: l.strategy_name for l in session.execute(select(Lot)).scalars()}
    assert lots == {
        201: "Put credit spread", 202: "Put credit spread",
        211: "Short strangle", 212: "Short strangle",
    }
    # closes inherit from their lot
    closes = session.execute(select(LotClose)).scalars().all()
    assert {c.lot_id: c.strategy_name for c in closes} == lots


def test_strategies_view_reads_the_stored_name(session):
    run_pipeline(session, _vertical_and_strangle())
    names = {r.open_order_id: r.strategy_name for r in strategies(session)}
    assert names == {7001: "Put credit spread", 7011: "Short strangle"}


def test_realized_pnl_groups_by_strategy(session):
    run_pipeline(session, _vertical_and_strangle())
    by_strategy = {r.group: r for r in realized_pnl(session, group_by="strategy")}
    assert set(by_strategy) == {"Put credit spread", "Short strangle"}
    # spread: +400 on the short leg, -250 on the long leg
    assert by_strategy["Put credit spread"].realized_pnl == Decimal("150")
    # strangle: +300 on the put, +175 on the call
    assert by_strategy["Short strangle"].realized_pnl == Decimal("475")
    assert by_strategy["Put credit spread"].closes == 2

    total = sum(r.realized_pnl for r in realized_pnl(session, group_by="underlying"))
    assert sum(r.realized_pnl for r in by_strategy.values()) == total


def test_credits_group_by_strategy_counts_each_transaction_once(session):
    run_pipeline(session, _vertical_and_strangle())
    rows = {r.group: r for r in credits_collected(session, group_by="strategy")}
    assert set(rows) == {"Put credit spread", "Short strangle"}
    # both the opening and the closing transactions land on the same strategy
    assert rows["Put credit spread"].trades == 4
    assert rows["Short strangle"].trades == 4
    # 500 - 300 - 100 + 50
    assert rows["Put credit spread"].credits == Decimal("150")
    # 400 + 200 - 100 - 25
    assert rows["Short strangle"].credits == Decimal("475")

    by_underlying = credits_collected(session, group_by="underlying")
    assert sum(r.credits for r in rows.values()) == sum(r.credits for r in by_underlying)
    assert sum(r.trades for r in rows.values()) == sum(r.trades for r in by_underlying)


def test_one_closing_transaction_over_several_lots_is_not_double_counted(session):
    """A close spanning two lots produces two lot_closes rows; the credit for
    that single transaction must still be counted once."""
    payloads = [
        make_txn(txn_id=301, action="Sell to Open", symbol="XSP   260320P00560000",
                 instrument_type="Equity Option", quantity=1, price=4.00,
                 value=400.0, value_effect="Credit",
                 executed_at="2026-01-05T15:00:00+00:00", **{"order-id": 7101}),
        make_txn(txn_id=302, action="Sell to Open", symbol="XSP   260320P00560000",
                 instrument_type="Equity Option", quantity=1, price=5.00,
                 value=500.0, value_effect="Credit",
                 executed_at="2026-01-06T15:00:00+00:00", **{"order-id": 7102}),
        make_txn(txn_id=303, action="Buy to Close", symbol="XSP   260320P00560000",
                 instrument_type="Equity Option", quantity=2, price=1.00,
                 value=200.0, value_effect="Debit",
                 executed_at="2026-02-05T15:00:00+00:00", **{"order-id": 7103}),
    ]
    run_pipeline(session, payloads)
    assert len(session.execute(select(LotClose)).scalars().all()) == 2

    rows = credits_collected(session, group_by="strategy")
    assert [(r.group, r.trades, r.credits) for r in rows] == [
        ("Short put", 3, Decimal("700")),
    ]


def test_strategy_filter_narrows_the_close_side_queries(session):
    run_pipeline(session, _vertical_and_strangle())
    rows = strategies(session, strategy="Short strangle")
    assert [r.open_order_id for r in rows] == [7011]

    by_underlying = realized_pnl(session, strategy="Short strangle")
    assert [(r.group, r.realized_pnl) for r in by_underlying] == [("XSP", Decimal("475"))]
    assert realized_pnl(session, strategy="No such strategy") == []


def test_list_credit_transactions_matches_the_grouped_totals(session):
    run_pipeline(session, _vertical_and_strangle())
    for row in credits_collected(session, group_by="strategy"):
        txns, count, total = list_credit_transactions(session, strategy=row.group)
        assert (count, total) == (row.trades, row.credits)
        assert {t.strategy for t in txns} == {row.group}
    # newest first
    dates = [t.executed_at for t in list_credit_transactions(session)[0]]
    assert dates == sorted(dates, reverse=True)


def test_credit_transactions_of_a_close_spanning_lots_are_listed_once(session):
    """The listing behind a credits row inherits the no-double-count rule."""
    payloads = [
        make_txn(txn_id=301, action="Sell to Open", symbol="XSP   260320P00560000",
                 instrument_type="Equity Option", quantity=1, price=4.00,
                 value=400.0, value_effect="Credit",
                 executed_at="2026-01-05T15:00:00+00:00", **{"order-id": 7101}),
        make_txn(txn_id=302, action="Sell to Open", symbol="XSP   260320P00560000",
                 instrument_type="Equity Option", quantity=1, price=5.00,
                 value=500.0, value_effect="Credit",
                 executed_at="2026-01-06T15:00:00+00:00", **{"order-id": 7102}),
        make_txn(txn_id=303, action="Buy to Close", symbol="XSP   260320P00560000",
                 instrument_type="Equity Option", quantity=2, price=1.00,
                 value=200.0, value_effect="Debit",
                 executed_at="2026-02-05T15:00:00+00:00", **{"order-id": 7103}),
    ]
    run_pipeline(session, payloads)
    txns, count, total = list_credit_transactions(session, strategy="Short put")
    assert [t.txn_id for t in txns] == [303, 302, 301]
    assert (count, total) == (3, Decimal("700"))
