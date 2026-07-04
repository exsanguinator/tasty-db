from datetime import date
from decimal import Decimal

from sqlalchemy import select

from tastydb.models import CloseReason, LotClose, OpenLot, Side

from .conftest import make_txn, run_pipeline


def _closes(session) -> list[LotClose]:
    return session.execute(select(LotClose).order_by(LotClose.close_id)).scalars().all()


def _lots(session) -> list[OpenLot]:
    return session.execute(select(OpenLot).order_by(OpenLot.lot_id)).scalars().all()


def test_fifo_split_close_across_lots(session):
    run_pipeline(
        session,
        [
            make_txn(action="Buy to Open", quantity=60, price=10.0,
                     executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Buy to Open", quantity=40, price=11.0,
                     executed_at="2024-01-03T15:00:00+00:00"),
            make_txn(action="Sell to Close", quantity=80, price=12.0, commission=2.0,
                     executed_at="2024-02-01T15:00:00+00:00"),
        ],
    )
    closes = _closes(session)
    assert len(closes) == 2  # one close txn split across two lots
    first, second = closes
    assert first.quantity_closed == 60 and first.open_price == Decimal("10")
    assert second.quantity_closed == 20 and second.open_price == Decimal("11")
    # close fees split pro-rata: 2.00 * 60/80 and * 20/80
    assert first.close_fees == Decimal("1.5")
    assert second.close_fees == Decimal("0.5")
    assert first.realized_pnl == (12 - 10) * 60 - Decimal("1.5")
    assert second.realized_pnl == (12 - 11) * 20 - Decimal("0.5")
    assert first.hold_days == 30

    lots = _lots(session)
    assert lots[0].remaining_quantity == 0
    assert lots[1].remaining_quantity == 20  # partial close decrements, keeps the row
    assert lots[1].original_quantity == 40


def test_lifo_order(session):
    run_pipeline(
        session,
        [
            make_txn(action="Buy to Open", quantity=60, price=10.0,
                     executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Buy to Open", quantity=40, price=11.0,
                     executed_at="2024-01-03T15:00:00+00:00"),
            make_txn(action="Sell to Close", quantity=50, price=12.0,
                     executed_at="2024-02-01T15:00:00+00:00"),
        ],
        method="lifo",
    )
    closes = _closes(session)
    assert closes[0].open_price == Decimal("11") and closes[0].quantity_closed == 40
    assert closes[1].open_price == Decimal("10") and closes[1].quantity_closed == 10


def test_short_option_lifecycle_with_expiration_txn(session):
    opt = "SPY   240119P00450000"
    run_pipeline(
        session,
        [
            make_txn(action="Sell to Open", symbol=opt, underlying="SPY",
                     instrument_type="Equity Option", quantity=2, price=1.50,
                     commission=2.0, executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Buy to Close", symbol=opt, underlying="SPY",
                     instrument_type="Equity Option", quantity=1, price=0.50,
                     commission=1.0, executed_at="2024-01-10T15:00:00+00:00"),
            make_txn(txn_type="Receive Deliver", sub_type="Expiration", symbol=opt,
                     underlying="SPY", instrument_type="Equity Option", quantity=1,
                     executed_at="2024-01-19T22:00:00+00:00"),
        ],
    )
    closes = _closes(session)
    assert len(closes) == 2
    btc, expiry = closes
    assert btc.side == Side.short
    # short: (open 1.50 - close 0.50) * 1 * 100 minus 1.00 open-fee share minus 1.00 close fee
    assert btc.realized_pnl == Decimal("98")
    assert expiry.close_reason == CloseReason.expiration
    assert expiry.close_price == 0
    assert expiry.broker_close_txn_id is not None  # real broker expiration txn
    assert expiry.realized_pnl == Decimal("149")  # 150 premium minus 1.00 open-fee share
    assert _lots(session)[0].remaining_quantity == 0


def test_assignment_links_new_stock_lot(session):
    opt = "SPY   240119P00450000"
    run_pipeline(
        session,
        [
            make_txn(action="Sell to Open", symbol=opt, underlying="SPY",
                     instrument_type="Equity Option", quantity=1, price=2.0,
                     executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(txn_type="Receive Deliver", sub_type="Assignment", symbol=opt,
                     underlying="SPY", instrument_type="Equity Option", quantity=1,
                     executed_at="2024-01-19T22:00:00+00:00"),
            make_txn(txn_type="Receive Deliver", action="Buy to Open", symbol="SPY",
                     instrument_type="Equity", quantity=100, price=450.0,
                     executed_at="2024-01-19T22:00:00+00:00"),
        ],
    )
    closes = _closes(session)
    assert len(closes) == 1
    option_close = closes[0]
    assert option_close.close_reason == CloseReason.assignment
    assert option_close.close_price == 0
    assert option_close.realized_pnl == Decimal("200")  # full premium kept

    stock_lots = [l for l in _lots(session) if l.symbol == "SPY"]
    assert len(stock_lots) == 1
    stock = stock_lots[0]
    assert stock.open_price == Decimal("450")
    assert stock.remaining_quantity == 100
    assert stock.side == Side.long
    assert option_close.linked_lot_id == stock.lot_id


def test_covered_call_assignment_closes_stock_with_assignment_reason(session):
    opt = "AAPL  240119C00012000"
    run_pipeline(
        session,
        [
            make_txn(action="Buy to Open", symbol="AAPL", quantity=100, price=10.0,
                     executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(action="Sell to Open", symbol=opt, underlying="AAPL",
                     instrument_type="Equity Option", quantity=1, price=1.0,
                     executed_at="2024-01-03T15:00:00+00:00"),
            make_txn(txn_type="Receive Deliver", sub_type="Assignment", symbol=opt,
                     underlying="AAPL", instrument_type="Equity Option", quantity=1,
                     executed_at="2024-01-19T22:00:00+00:00"),
            make_txn(txn_type="Receive Deliver", action="Sell to Close", symbol="AAPL",
                     instrument_type="Equity", quantity=100, price=12.0,
                     executed_at="2024-01-19T22:00:00+00:00"),
        ],
    )
    closes = _closes(session)
    stock_close = next(c for c in closes if c.symbol == "AAPL")
    option_close = next(c for c in closes if c.symbol == opt)
    # stock called away at the strike: reason inherited from the assignment
    assert stock_close.close_reason == CloseReason.assignment
    assert stock_close.realized_pnl == Decimal("200")  # (12-10)*100
    assert option_close.realized_pnl == Decimal("100")  # premium
    # no new lot was opened by the delivery -> nothing to link
    assert option_close.linked_lot_id is None


def test_cash_settlement_long_exercise(session):
    opt = "SPXW  240119C04700000"
    run_pipeline(
        session,
        [
            make_txn(action="Buy to Open", symbol=opt, underlying="SPX",
                     instrument_type="Equity Option", quantity=1, price=5.0,
                     executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(txn_type="Receive Deliver", sub_type="Cash Settled Exercise",
                     symbol=opt, underlying="SPX", instrument_type="Equity Option",
                     quantity=1, value=750.0, value_effect="Credit",
                     executed_at="2024-01-19T22:00:00+00:00"),
        ],
    )
    closes = _closes(session)
    assert len(closes) == 1
    close = closes[0]
    assert close.close_reason == CloseReason.cash_settlement
    # settlement cash 750 -> effective close price 7.50, NOT a worthless expiry
    assert close.close_price == Decimal("7.5")
    assert close.realized_pnl == Decimal("250")  # (7.5 - 5.0) * 100
    assert close.linked_lot_id is None


def test_cash_settlement_short_assignment(session):
    opt = "SPXW  240119P04700000"
    run_pipeline(
        session,
        [
            make_txn(action="Sell to Open", symbol=opt, underlying="SPX",
                     instrument_type="Equity Option", quantity=1, price=6.0,
                     executed_at="2024-01-02T15:00:00+00:00"),
            make_txn(txn_type="Receive Deliver", sub_type="Cash Settled Assignment",
                     symbol=opt, underlying="SPX", instrument_type="Equity Option",
                     quantity=1, value=1000.0, value_effect="Debit",
                     executed_at="2024-01-19T22:00:00+00:00"),
        ],
    )
    close = _closes(session)[0]
    assert close.close_reason == CloseReason.cash_settlement
    assert close.close_price == Decimal("10")
    assert close.realized_pnl == Decimal("-400")  # short: (6 - 10) * 100


def test_worthless_expiration_sweep_without_broker_txn(session):
    opt = "QQQ   240621P00400000"
    stats = run_pipeline(
        session,
        [
            make_txn(action="Sell to Open", symbol=opt, underlying="QQQ",
                     instrument_type="Equity Option", quantity=1, price=3.0,
                     executed_at="2024-06-01T15:00:00+00:00"),
            # unrelated later activity moves the sync watermark past expiry+grace
            make_txn(txn_type="Money Movement", sub_type="Deposit", symbol=None,
                     instrument_type=None, quantity=0,
                     executed_at="2024-07-15T15:00:00+00:00"),
        ],
    )
    assert stats["expired_worthless"] == 1
    close = _closes(session)[0]
    assert close.close_reason == CloseReason.expiration
    assert close.broker_close_txn_id is None  # synthetic: no broker txn exists
    assert close.close_price == 0
    assert close.close_date.date() == date(2024, 6, 21)
    assert close.realized_pnl == Decimal("300")


def test_sweep_respects_grace_window(session):
    opt = "QQQ   240621P00400000"
    stats = run_pipeline(
        session,
        [
            make_txn(action="Sell to Open", symbol=opt, underlying="QQQ",
                     instrument_type="Equity Option", quantity=1, price=3.0,
                     executed_at="2024-06-01T15:00:00+00:00"),
            # watermark only 2 days past expiration: settlement txns may still post
            make_txn(txn_type="Money Movement", sub_type="Deposit", symbol=None,
                     instrument_type=None, quantity=0,
                     executed_at="2024-06-23T15:00:00+00:00"),
        ],
    )
    assert stats["expired_worthless"] == 0
    assert _lots(session)[0].remaining_quantity == 1


def test_futures_multiplier_and_plain_buy_sell(session):
    run_pipeline(
        session,
        [
            make_txn(action="Buy", symbol="/ESZ4", underlying="/ESZ4",
                     instrument_type="Future", quantity=1, price=5000.0,
                     executed_at="2024-10-01T15:00:00+00:00"),
            make_txn(action="Sell", symbol="/ESZ4", underlying="/ESZ4",
                     instrument_type="Future", quantity=1, price=5010.0,
                     executed_at="2024-10-02T15:00:00+00:00"),
        ],
    )
    close = _closes(session)[0]
    assert close.multiplier == Decimal("50")
    assert close.realized_pnl == Decimal("500")  # 10 points * $50
    lot = _lots(session)[0]
    assert lot.futures_contract_code == "ES"


def test_future_option_multiplier(session):
    sym = "./ESZ4 EW4U4 241227P05900"
    run_pipeline(
        session,
        [
            make_txn(action="Buy to Open", symbol=sym, underlying="/ESZ4",
                     instrument_type="Future Option", quantity=1, price=10.0,
                     executed_at="2024-10-01T15:00:00+00:00"),
            make_txn(action="Sell to Close", symbol=sym, underlying="/ESZ4",
                     instrument_type="Future Option", quantity=1, price=20.0,
                     executed_at="2024-10-02T15:00:00+00:00"),
        ],
    )
    close = _closes(session)[0]
    assert close.multiplier == Decimal("50")
    assert close.realized_pnl == Decimal("500")


def test_plain_buy_crosses_through_zero(session):
    run_pipeline(
        session,
        [
            make_txn(action="Sell", symbol="/GCZ4", underlying="/GCZ4",
                     instrument_type="Future", quantity=1, price=2000.0,
                     executed_at="2024-10-01T15:00:00+00:00"),
            make_txn(action="Buy", symbol="/GCZ4", underlying="/GCZ4",
                     instrument_type="Future", quantity=3, price=1990.0,
                     executed_at="2024-10-02T15:00:00+00:00"),
        ],
    )
    close = _closes(session)[0]
    assert close.side == Side.short
    assert close.realized_pnl == Decimal("1000")  # (2000-1990) * 1 * 100/oz
    open_lots = [l for l in _lots(session) if l.remaining_quantity > 0]
    assert len(open_lots) == 1
    assert open_lots[0].side == Side.long
    assert open_lots[0].remaining_quantity == 2
    assert open_lots[0].open_price == Decimal("1990")


def test_reversal_pair_is_excluded(session):
    run_pipeline(
        session,
        [
            make_txn(txn_id=10, action="Buy to Open", quantity=100, price=10.0),
            make_txn(txn_id=11, action="Buy to Open", quantity=100, price=10.0,
                     **{"reverses-id": 10}),
        ],
    )
    assert _lots(session) == []
    assert _closes(session) == []


def test_rebuild_is_deterministic_and_rerunnable(session):
    payloads = [
        make_txn(action="Buy to Open", quantity=100, price=10.0,
                 executed_at="2024-01-02T15:00:00+00:00"),
        make_txn(action="Sell to Close", quantity=100, price=12.0,
                 executed_at="2024-02-01T15:00:00+00:00"),
    ]
    first = run_pipeline(session, payloads)
    second = run_pipeline(session, [])  # no new payloads; pure reprocess
    assert first["closes"] == second["closes"] == 1
    assert len(_closes(session)) == 1  # no duplicates from reprocessing
