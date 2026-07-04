"""Regression tests for transaction shapes observed in real production data
(2026-07 backfill) that the docs don't spell out."""

from decimal import Decimal

from sqlalchemy import select

from tastydb.models import (
    CloseReason,
    InstrumentMeta,
    LotClose,
    OpenLot,
    ProcessingStatus,
    RawTransaction,
)
from tastydb.symbology import parse_future_option_symbol

from .conftest import make_txn, run_pipeline


def test_parse_future_option_with_full_width_head():
    # 5-char future symbol leaves no space between head fields
    parsed = parse_future_option_symbol("./MESM1EX3K1 210521P2920")
    assert parsed.underlying_future == "/MESM1"
    assert parsed.product_code == "MES"
    assert parsed.strike == Decimal("2920")


def test_cash_settlement_double_transaction_pattern(session):
    """tastytrade posts cash settlement as TWO txns per leg: the cash-carrying
    'Cash Settled Exercise/Assignment' plus a value-0 'Exercise'/'Assignment'
    removal. Only the cash txn must close the lot; the removal is redundant."""
    long_leg = "XSP   260408C00659000"
    short_leg = "XSP   260408C00658000"
    run_pipeline(
        session,
        [
            make_txn(action="Sell to Open", symbol=short_leg, underlying="XSP",
                     instrument_type="Equity Option", quantity=2, price=4.42, value=884.0,
                     value_effect="Credit", executed_at="2026-04-08T14:00:00+00:00"),
            make_txn(action="Buy to Open", symbol=long_leg, underlying="XSP",
                     instrument_type="Equity Option", quantity=2, price=3.92, value=784.0,
                     value_effect="Debit", executed_at="2026-04-08T14:00:01+00:00"),
            make_txn(txn_type="Receive Deliver", sub_type="Cash Settled Exercise",
                     symbol=long_leg, underlying="XSP", instrument_type="Equity Option",
                     quantity=2, price=659.0, value=3856.0, value_effect="Credit",
                     executed_at="2026-04-08T21:00:00+00:00"),
            make_txn(txn_type="Receive Deliver", sub_type="Exercise", symbol=long_leg,
                     underlying="XSP", instrument_type="Equity Option", quantity=2,
                     executed_at="2026-04-08T21:00:00+00:00"),
            make_txn(txn_type="Receive Deliver", sub_type="Cash Settled Assignment",
                     symbol=short_leg, underlying="XSP", instrument_type="Equity Option",
                     quantity=2, price=658.0, value=4056.0, value_effect="Debit",
                     executed_at="2026-04-08T21:00:00+00:00"),
            make_txn(txn_type="Receive Deliver", sub_type="Assignment", symbol=short_leg,
                     underlying="XSP", instrument_type="Equity Option", quantity=2,
                     executed_at="2026-04-08T21:00:00+00:00"),
        ],
    )
    closes = session.execute(select(LotClose)).scalars().all()
    assert len(closes) == 2  # one per leg — removals produced nothing
    by_symbol = {c.symbol: c for c in closes}

    long_close = by_symbol[long_leg]
    assert long_close.close_reason == CloseReason.cash_settlement
    # settlement cash 3856 over 2 contracts -> 19.28, NOT the 659 strike in `price`
    assert long_close.close_price == Decimal("19.28")
    assert long_close.realized_pnl == Decimal("3072")  # (19.28 - 3.92) * 2 * 100

    short_close = by_symbol[short_leg]
    assert short_close.close_price == Decimal("20.28")
    assert short_close.realized_pnl == Decimal("-3172")  # (4.42 - 20.28) * 2 * 100

    # no phantom lots from the removal legs
    lots = session.execute(select(OpenLot)).scalars().all()
    assert len(lots) == 2
    assert all(lot.remaining_quantity == 0 for lot in lots)

    removals = session.execute(
        select(RawTransaction).where(
            RawTransaction.transaction_sub_type.in_(["Exercise", "Assignment"])
        )
    ).scalars().all()
    assert all(t.processing_status == ProcessingStatus.ignored for t in removals)
    assert all("cash settlement" in t.processing_note for t in removals)


def test_multiplier_derived_from_transaction_value(session):
    """A future option on an unknown product still gets the right multiplier,
    derived from value = price * qty * multiplier (observed: MES options,
    26.25 x 5 = 131.25)."""
    sym = "./MESM1EX3K1 210521P2920"
    run_pipeline(
        session,
        [
            make_txn(action="Sell to Open", symbol=sym, underlying="/MESM1",
                     instrument_type="Future Option", quantity=1, price=26.25,
                     value=131.25, value_effect="Credit",
                     executed_at="2021-04-20T15:00:00+00:00"),
            make_txn(action="Buy to Close", symbol=sym, underlying="/MESM1",
                     instrument_type="Future Option", quantity=1, price=13.0,
                     value=65.0, value_effect="Debit",
                     executed_at="2021-05-10T15:00:00+00:00"),
        ],
    )
    close = session.execute(select(LotClose)).scalar_one()
    assert close.multiplier == Decimal("5")
    assert close.realized_pnl == Decimal("66.25")  # (26.25 - 13.00) * 1 * 5


def test_stale_fallback_meta_is_rederived(session):
    """Fallback-sourced cache rows are recomputed on access, so a parser or
    contract-table fix corrects previously cached bad values on the next run."""
    sym = "./MESM1EX3K1 210521P2920"
    session.add(InstrumentMeta(
        symbol=sym, asset_type="future_option", multiplier=Decimal("1"),
        source="fallback",
    ))
    session.commit()
    run_pipeline(
        session,
        [
            make_txn(action="Buy to Open", symbol=sym, underlying="/MESM1",
                     instrument_type="Future Option", quantity=1, price=10.0,
                     value=50.0, value_effect="Debit",
                     executed_at="2021-04-20T15:00:00+00:00"),
        ],
    )
    lot = session.execute(select(OpenLot)).scalar_one()
    assert lot.multiplier == Decimal("5")
    assert lot.futures_contract_code == "MES"


def test_future_option_multiplier_from_api_payload():
    from tastydb.instruments import future_option_multiplier

    # real ES weekly payload shape: multiplier field is a useless "1.0"
    assert future_option_multiplier(
        {"multiplier": "1.0", "notional-value": "0.5", "display-factor": "0.01"}
    ) == Decimal("50")
    assert future_option_multiplier(
        {"notional-value": "12.5", "display-factor": "0.000001"}
    ) == Decimal("12500000")  # 6J
    assert future_option_multiplier({"notional-value": "0.5"}) is None
    assert future_option_multiplier({"notional-value": "0", "display-factor": "0.01"}) is None


def test_cached_api_future_option_row_is_self_healed(session):
    """Rows cached with the bogus payload `multiplier` get corrected from the
    stored payload's notional-value/display-factor on next access."""
    sym = "./ESM9 EWH9  190329P2700"
    session.add(InstrumentMeta(
        symbol=sym, asset_type="future_option", multiplier=Decimal("1"),
        source="api",
        payload={"multiplier": "1.0", "notional-value": "0.5", "display-factor": "0.01"},
    ))
    session.commit()
    run_pipeline(
        session,
        [
            make_txn(action="Sell to Open", symbol=sym, underlying="/ESM9",
                     instrument_type="Future Option", quantity=1, price=10.0,
                     value=500.0, value_effect="Credit",
                     executed_at="2019-03-01T15:00:00+00:00"),
            make_txn(action="Buy to Close", symbol=sym, underlying="/ESM9",
                     instrument_type="Future Option", quantity=1, price=4.0,
                     value=200.0, value_effect="Debit",
                     executed_at="2019-03-15T15:00:00+00:00"),
        ],
    )
    close = session.execute(select(LotClose)).scalar_one()
    assert close.multiplier == Decimal("50")
    assert close.realized_pnl == Decimal("300")  # (10 - 4) * 1 * 50
    meta = session.get(InstrumentMeta, sym)
    assert meta.multiplier == Decimal("50")


def test_manual_meta_rows_are_never_recomputed(session):
    sym = "./MESM1EX3K1 210521P2920"
    session.add(InstrumentMeta(
        symbol=sym, asset_type="future_option", multiplier=Decimal("7"),
        source="manual",
    ))
    session.commit()
    run_pipeline(
        session,
        [
            make_txn(action="Buy to Open", symbol=sym, underlying="/MESM1",
                     instrument_type="Future Option", quantity=1, price=10.0,
                     value=50.0, value_effect="Debit",
                     executed_at="2021-04-20T15:00:00+00:00"),
        ],
    )
    lot = session.execute(select(OpenLot)).scalar_one()
    assert lot.multiplier == Decimal("7")  # pinned value wins
