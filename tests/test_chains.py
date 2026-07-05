"""Roll-chain grouping: orders linked by a roll (one order id closing old lots
and opening new ones) share a chain_id rooted at the earliest opening order."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from tastydb.analytics import strategies
from tastydb.chains import chain_detail, chains
from tastydb.models import Lot, LotClose

from .conftest import make_txn, run_pipeline


def _roll_payloads():
    """A short-put campaign: open (order 5001), roll down-and-out (order 5002),
    final buyback (order 5003)."""
    return [
        make_txn(txn_id=101, action="Sell to Open", symbol="XSP   260320P00560000",
                 instrument_type="Equity Option", quantity=2, price=3.00,
                 executed_at="2026-01-05T15:00:00+00:00", **{"order-id": 5001}),
        make_txn(txn_id=102, action="Buy to Close", symbol="XSP   260320P00560000",
                 instrument_type="Equity Option", quantity=2, price=5.00,
                 executed_at="2026-02-10T15:00:00+00:00", **{"order-id": 5002}),
        make_txn(txn_id=103, action="Sell to Open", symbol="XSP   260417P00550000",
                 instrument_type="Equity Option", quantity=2, price=7.20,
                 executed_at="2026-02-10T15:00:00+00:00", **{"order-id": 5002}),
        make_txn(txn_id=104, action="Buy to Close", symbol="XSP   260417P00550000",
                 instrument_type="Equity Option", quantity=2, price=1.10,
                 executed_at="2026-03-15T15:00:00+00:00", **{"order-id": 5003}),
    ]


def test_roll_links_lots_and_closes_into_one_chain(session):
    run_pipeline(session, _roll_payloads())
    lots = session.execute(select(Lot)).scalars().all()
    assert {l.lot_id: l.chain_id for l in lots} == {101: 5001, 103: 5001}
    closes = session.execute(select(LotClose)).scalars().all()
    assert {c.chain_id for c in closes} == {5001}


def test_plain_open_close_gets_no_chain(session):
    run_pipeline(session, [
        make_txn(txn_id=111, action="Buy to Open", symbol="AAPL", quantity=10,
                 price=10.0, executed_at="2026-01-02T15:00:00+00:00",
                 **{"order-id": 5101}),
        make_txn(txn_id=112, action="Sell to Close", symbol="AAPL", quantity=10,
                 price=12.0, executed_at="2026-02-02T15:00:00+00:00",
                 **{"order-id": 5102}),
    ])
    lot = session.execute(select(Lot)).scalar_one()
    assert lot.chain_id is None
    assert chains(session) == []


def test_chain_summary_and_detail_math(session):
    run_pipeline(session, _roll_payloads())
    rows = chains(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.chain_id == 5001
    assert row.underlying_symbol == "XSP"
    assert row.orders == 3
    assert row.rolls == 1
    assert not row.is_open
    # leg 1: (5.00-3.00)*2*100*-1 = -400; leg 2: (1.10-7.20)*2*100*-1 = +1220
    assert row.realized_pnl == Decimal("820")
    assert row.days_in_trade == (date(2026, 3, 15) - date(2026, 1, 5)).days

    (detail,) = chain_detail(session, 5001)
    assert [s.kind for s in detail.steps] == ["open", "roll", "close"]
    # credits: +600 open, -1000+1440 roll, -220 close
    assert [s.running_cash for s in detail.steps] == [
        Decimal("600"), Decimal("1040"), Decimal("820"),
    ]
    # fully closed: net cash == realized PnL
    assert detail.net_cash == detail.realized_pnl == Decimal("820")


def test_one_order_rolling_two_positions_merges_chains(session):
    run_pipeline(session, [
        make_txn(txn_id=201, action="Sell to Open", symbol="XSP   260320P00560000",
                 instrument_type="Equity Option", quantity=1, price=2.0,
                 executed_at="2026-01-05T15:00:00+00:00", **{"order-id": 6001}),
        make_txn(txn_id=202, action="Sell to Open", symbol="XSP   260320P00555000",
                 instrument_type="Equity Option", quantity=1, price=3.0,
                 executed_at="2026-01-08T15:00:00+00:00", **{"order-id": 6002}),
        # one order closes both strikes and opens the replacement
        make_txn(txn_id=203, action="Buy to Close", symbol="XSP   260320P00560000",
                 instrument_type="Equity Option", quantity=1, price=1.0,
                 executed_at="2026-02-01T15:00:00+00:00", **{"order-id": 6003}),
        make_txn(txn_id=204, action="Buy to Close", symbol="XSP   260320P00555000",
                 instrument_type="Equity Option", quantity=1, price=1.0,
                 executed_at="2026-02-01T15:00:00+00:00", **{"order-id": 6003}),
        make_txn(txn_id=205, action="Sell to Open", symbol="XSP   260417P00550000",
                 instrument_type="Equity Option", quantity=2, price=4.0,
                 executed_at="2026-02-01T15:00:00+00:00", **{"order-id": 6003}),
    ])
    lots = session.execute(select(Lot)).scalars().all()
    assert {l.chain_id for l in lots} == {6001}  # root = earliest opening order
    rows = chains(session)
    assert len(rows) == 1 and rows[0].orders == 3 and rows[0].is_open


def test_pairs_order_does_not_cross_link_underlyings(session):
    run_pipeline(session, [
        # one order opens positions in two underlyings
        make_txn(txn_id=301, action="Sell to Open", symbol="AAPL  260320P00150000",
                 instrument_type="Equity Option", quantity=1, price=2.0,
                 executed_at="2026-01-05T15:00:00+00:00", **{"order-id": 7001}),
        make_txn(txn_id=302, action="Sell to Open", symbol="MSFT  260320P00400000",
                 instrument_type="Equity Option", quantity=1, price=3.0,
                 executed_at="2026-01-05T15:00:00+00:00", **{"order-id": 7001}),
        # only the AAPL leg gets rolled
        make_txn(txn_id=303, action="Buy to Close", symbol="AAPL  260320P00150000",
                 instrument_type="Equity Option", quantity=1, price=3.0,
                 executed_at="2026-02-01T15:00:00+00:00", **{"order-id": 7002}),
        make_txn(txn_id=304, action="Sell to Open", symbol="AAPL  260417P00145000",
                 instrument_type="Equity Option", quantity=1, price=4.0,
                 executed_at="2026-02-01T15:00:00+00:00", **{"order-id": 7002}),
    ])
    by_lot = {l.lot_id: l.chain_id for l in session.execute(select(Lot)).scalars()}
    assert by_lot == {301: 7001, 304: 7001, 302: None}
    details = chain_detail(session, 7001)
    assert len(details) == 1 and details[0].underlying_symbol == "AAPL"


def test_sweep_close_inherits_chain_through_its_lot(session):
    run_pipeline(session, [
        make_txn(txn_id=401, action="Sell to Open", symbol="AAPL  240621P00150000",
                 instrument_type="Equity Option", quantity=1, price=2.0,
                 executed_at="2024-05-01T15:00:00+00:00", **{"order-id": 8001}),
        make_txn(txn_id=402, action="Buy to Close", symbol="AAPL  240621P00150000",
                 instrument_type="Equity Option", quantity=1, price=1.5,
                 executed_at="2024-06-01T15:00:00+00:00", **{"order-id": 8002}),
        make_txn(txn_id=403, action="Sell to Open", symbol="AAPL  240719P00145000",
                 instrument_type="Equity Option", quantity=1, price=2.5,
                 executed_at="2024-06-01T15:00:00+00:00", **{"order-id": 8002}),
        # unrelated later txn advances the sweep's as-of past the expiry+grace
        make_txn(txn_id=404, action="Buy to Open", symbol="MSFT", quantity=10,
                 price=100.0, executed_at="2024-08-15T15:00:00+00:00",
                 **{"order-id": 8003}),
    ])
    sweep = session.execute(
        select(LotClose).where(LotClose.broker_close_txn_id.is_(None))
    ).scalar_one()
    assert sweep.close_order_id is None
    assert sweep.chain_id == 8001

    (detail,) = chain_detail(session, 8001)
    assert [s.kind for s in detail.steps] == ["open", "roll", "expiration"]
    assert not detail.is_open


def test_strategies_link_to_their_chain(session):
    run_pipeline(session, _roll_payloads())
    rows = strategies(session)
    assert {r.open_order_id: r.chain_id for r in rows} == {5001: 5001, 5002: 5001}


def test_date_filter_selects_chains_but_totals_span_campaign(session):
    run_pipeline(session, _roll_payloads())
    # closed 2026-03-15: out of range -> hidden
    assert chains(session, start=date(2026, 4, 1)) == []
    # in range by its roll close, totals still whole-campaign
    rows = chains(session, start=date(2026, 2, 1), end=date(2026, 2, 28))
    assert len(rows) == 1 and rows[0].realized_pnl == Decimal("820")


def test_dashboard_chain_pages(tmp_path):
    from .test_phase4 import _dashboard_client

    client = _dashboard_client(_roll_payloads(), tmp_path)

    listing = client.get("/chains")
    assert listing.status_code == 200
    assert "/chain/5001" in listing.text

    page = client.get("/chain/5001")
    assert page.status_code == 200
    assert "Chain 5001" in page.text
    assert "roll" in page.text
    assert "XSP   260417P00550000" in page.text

    assert client.get("/chain/999999").status_code == 200  # not-found page, no 500

    strategies_page = client.get("/strategies")
    assert "/chain/5001" in strategies_page.text

    lot_page = client.get("/lot/103")
    assert "/chain/5001" in lot_page.text
