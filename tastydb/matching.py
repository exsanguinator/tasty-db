"""Lot matching: replays classified position events into lots/lot_closes.

Design decisions:

- Deterministic rebuild. `rebuild_lots` wipes derived tables and replays the
  full raw ledger in (executed_at, id) order. Raw transactions are the source
  of truth; deriving lots from scratch every run makes reprocessing after a
  matching-logic fix or a fee reconciliation trivially correct. Lot identity
  is stable across rebuilds: lot_id IS the opening broker transaction id.
  Close rows are stable via (lot_id, broker_close_txn_id); their close_id
  surrogate is internal only.

- Money hygiene: fee allocations and realized PnL are quantized to 4 decimal
  places (prices/multipliers to 8) so pro-rata splits never leak repeating
  decimals or float artifacts into the DB.

- Matching group. Lots are matched per (account, exact symbol, side) — exact
  symbol rather than just underlying+asset_type, because an option's symbol
  encodes strike/expiry and closes must never cross contracts. Within a group,
  order is FIFO by default, LIFO when configured.

- A close larger than the open book (history gap, e.g. backfill starting
  mid-position) logs a warning and opens a lot in the trade's own direction so
  the book stays consistent.

- Assignment/exercise linking: option-removal closes and delivery-leg lots are
  grouped by (account, underlying, transaction date). After replay, each
  removal close gets linked_lot_id -> the delivery lot it created (when one
  was opened), and delivery-leg closes inherit the removal reason (e.g. stock
  called away by a covered-call assignment books as reason=assignment).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .chains import assign_chains
from .classify import BUY, CLOSE, NET, OPEN, PositionEvent, classify_all
from .instruments import MetaProvider
from .models import (
    AssetType,
    CloseReason,
    LotClose,
    Lot,
    RawTransaction,
    SettlementType,
    Side,
)
from .structures import assign_strategy_names

log = logging.getLogger(__name__)

ZERO = Decimal("0")
Q_MONEY = Decimal("0.0001")  # fees / realized PnL
Q_PRICE = Decimal("0.00000001")  # prices / multipliers


class _LinkGroup:
    __slots__ = ("removal_closes", "removal_reason", "delivery_lots", "delivery_closes")

    def __init__(self):
        self.removal_closes: list[LotClose] = []
        self.removal_reason: CloseReason | None = None
        self.delivery_lots: list[Lot] = []
        self.delivery_closes: list[LotClose] = []


class Matcher:
    def __init__(self, session: Session, meta: MetaProvider, method: str = "fifo"):
        if method not in ("fifo", "lifo"):
            raise ValueError(f"match method must be 'fifo' or 'lifo', got {method!r}")
        self._session = session
        self._meta = meta
        self._method = method
        # (account, symbol, side) -> open lots in open order
        self._book: dict[tuple[str, str, Side], list[Lot]] = {}
        self._links: dict[tuple, _LinkGroup] = {}

    # -- event application ---------------------------------------------------

    def apply(self, event: PositionEvent) -> None:
        if event.kind == OPEN:
            side = Side.long if event.direction == BUY else Side.short
            self._open_lot(event, side, event.qty, event.fees)
        elif event.kind == CLOSE:
            self._apply_close(event)
        elif event.kind == NET:
            self._apply_net(event)
        else:  # pragma: no cover
            raise ValueError(f"unknown event kind {event.kind!r}")

    def _apply_close(self, event: PositionEvent) -> None:
        if event.direction == BUY:
            sides = [Side.short]
        elif event.direction == "sell":
            sides = [Side.long]
        else:
            # e.g. worthless expiration: remove whichever side is open
            sides = [
                s for s in (Side.long, Side.short)
                if self._book.get((event.account, event.symbol, s))
            ]
            if not sides:
                log.warning(
                    "txn %s: %s close of %s but no open lots; skipped",
                    event.txn_id, event.close_reason.value, event.symbol,
                )
                return

        remaining = event.qty
        for side in sides:
            if remaining <= ZERO:
                break
            remaining = self._close_against_book(event, side, remaining)

        if remaining > ZERO:
            if event.direction is None:
                log.warning(
                    "txn %s: %s of %s exceeds open book by %s; leftover dropped",
                    event.txn_id, event.close_reason.value, event.symbol, remaining,
                )
                return
            # explicit close with nothing (left) to close: history gap
            side = Side.short if event.direction == "sell" else Side.long
            log.warning(
                "txn %s: close of %s x%s had no matching open lot; opening %s lot "
                "(history likely starts mid-position)",
                event.txn_id, event.symbol, remaining, side.value,
            )
            self._open_lot(event, side, remaining, self._fee_share(event, remaining))

    def _apply_net(self, event: PositionEvent) -> None:
        """Plain Buy/Sell (and delivery legs): close the opposite side first,
        open the remainder — correctly handles crossing through zero."""
        close_side = Side.short if event.direction == BUY else Side.long
        remaining = self._close_against_book(event, close_side, event.qty)
        if remaining > ZERO:
            open_side = Side.long if event.direction == BUY else Side.short
            self._open_lot(event, open_side, remaining, self._fee_share(event, remaining))

    # -- primitives -----------------------------------------------------------

    def _resolve_multiplier(self, event: PositionEvent, meta) -> Decimal:
        """For options whose metadata came from fallback guessing, the broker's
        own numbers are more authoritative: an option trade's gross value is
        exactly price * qty * multiplier, so derive the multiplier from the
        transaction and pin it (source="derived"). Not applicable to outright
        futures, whose transaction value is settlement cash, not notional."""
        if (
            meta.source == "fallback"
            and event.asset_type in (AssetType.equity_option, AssetType.future_option)
            and event.price and event.price > ZERO
            and event.gross_value and event.qty > ZERO
        ):
            derived = (event.gross_value / (event.qty * event.price)).quantize(Q_PRICE)
            # >1% deviation guards against cents-rounding noise in `value`
            deviation = (
                abs(derived - meta.multiplier) / meta.multiplier
                if meta.multiplier > ZERO else Decimal("1")
            )
            if derived > ZERO and deviation > Decimal("0.01"):
                log.info(
                    "derived multiplier %s for %s from transaction value "
                    "(fallback guessed %s)", derived, event.symbol, meta.multiplier,
                )
                meta.multiplier = derived
                meta.source = "derived"
        return meta.multiplier

    def _open_lot(self, event: PositionEvent, side: Side, qty: Decimal, fees: Decimal) -> Lot:
        meta = self._meta.get(event.symbol, event.instrument_type)
        multiplier = self._resolve_multiplier(event, meta)
        lot = Lot(
            lot_id=event.txn_id,  # stable identity: the opening broker txn id
            broker_txn_id=event.txn_id,
            account_id=event.account,
            symbol=event.symbol,
            underlying_symbol=event.underlying,
            asset_type=event.asset_type,
            side=side,
            original_quantity=qty,
            remaining_quantity=qty,
            open_date=event.ts,
            open_price=event.price if event.price is not None else ZERO,
            open_fees=fees,
            multiplier=multiplier,
            strike=meta.strike,
            option_type=meta.option_type,
            expiration_date=meta.expiration_date,
            futures_contract_code=meta.contract_code,
            settlement_type=meta.settlement_type,
            open_order_id=event.order_id,
        )
        self._session.add(lot)
        self._book.setdefault((event.account, event.symbol, side), []).append(lot)
        if event.is_delivery:
            self._link_group(event).delivery_lots.append(lot)
        return lot

    def _close_against_book(self, event: PositionEvent, side: Side, qty: Decimal) -> Decimal:
        """Close up to `qty` against open lots; returns unfilled remainder."""
        key = (event.account, event.symbol, side)
        lots = self._book.get(key, [])
        remaining = qty
        while remaining > ZERO and lots:
            lot = lots[0] if self._method == "fifo" else lots[-1]
            take = min(lot.remaining_quantity, remaining)
            self._record_close(event, lot, side, take)
            lot.remaining_quantity -= take
            remaining -= take
            if lot.remaining_quantity <= ZERO:
                lots.remove(lot)
        return remaining

    def _fee_share(self, event: PositionEvent, qty: Decimal) -> Decimal:
        if not event.qty:
            return ZERO
        return (event.fees * qty / event.qty).quantize(Q_MONEY)

    def _close_price(self, event: PositionEvent, lot: Lot) -> Decimal:
        if event.close_reason == CloseReason.cash_settlement:
            # The broker transaction's value is the official exchange settlement
            # cash. Convert to an effective per-unit price so the standard PnL
            # formula applies; magnitude works for both sides (longs receive it,
            # shorts pay it) because side_sign handles the direction.
            if event.cash_value is None or event.qty == ZERO:
                return ZERO
            return (abs(event.cash_value) / (event.qty * lot.multiplier)).quantize(Q_PRICE)
        return event.price if event.price is not None else ZERO

    def _record_close(self, event: PositionEvent, lot: Lot, side: Side, qty: Decimal) -> LotClose:
        close_price = self._close_price(event, lot)
        open_fee_share = (
            (lot.open_fees * qty / lot.original_quantity).quantize(Q_MONEY)
            if lot.original_quantity else ZERO
        )
        close_fee_share = self._fee_share(event, qty)
        side_sign = 1 if side == Side.long else -1
        realized = (
            (close_price - lot.open_price) * qty * lot.multiplier * side_sign
            - open_fee_share
            - close_fee_share
        ).quantize(Q_MONEY)
        row = LotClose(
            lot=lot,
            broker_close_txn_id=event.txn_id,
            account_id=lot.account_id,
            symbol=lot.symbol,
            underlying_symbol=lot.underlying_symbol,
            asset_type=lot.asset_type,
            side=side,
            multiplier=lot.multiplier,
            quantity_closed=qty,
            open_date=lot.open_date,
            open_price=lot.open_price,
            open_fees=open_fee_share,
            close_date=event.ts,
            close_price=close_price,
            close_fees=close_fee_share,
            close_reason=event.close_reason,
            realized_pnl=realized,
            hold_days=(event.ts.date() - lot.open_date.date()).days,
            open_order_id=lot.open_order_id,
            close_order_id=event.order_id,
        )
        self._session.add(row)
        if event.is_removal:
            group = self._link_group(event)
            group.removal_closes.append(row)
            group.removal_reason = event.close_reason
        elif event.is_delivery:
            self._link_group(event).delivery_closes.append(row)
        return row

    # -- assignment/exercise linking ------------------------------------------

    def _link_group(self, event: PositionEvent) -> _LinkGroup:
        return self._links.setdefault(event.link_key, _LinkGroup())

    def finalize_links(self) -> None:
        for key, group in self._links.items():
            if group.removal_closes and group.delivery_lots:
                target = group.delivery_lots[0]
                if len(group.delivery_lots) > 1 or len(group.removal_closes) > 1:
                    log.info(
                        "multiple assignment/exercise events for %s on %s; "
                        "linking to the first delivery lot", key[1], key[2],
                    )
                for row in group.removal_closes:
                    row.linked_lot = target
            if group.removal_reason is not None:
                for row in group.delivery_closes:
                    row.close_reason = group.removal_reason

    # -- expiration sweep -------------------------------------------------------

    def expire_worthless(self, as_of: date, grace_days: int = 4) -> int:
        """Synthetically close lots whose expiration passed with no broker
        closing transaction (path 4a: physical, OTM/worthless). Runs after all
        real transactions — including cash settlements — have been applied, so
        anything still open past expiration+grace genuinely expired worthless.
        Cash-settled lots reaching this point are closed at 0 too (OTM expiry)
        but logged, since an ITM one would mean a missing settlement txn."""
        cutoff = as_of - timedelta(days=grace_days)
        swept = 0
        for lots in list(self._book.values()):
            for lot in list(lots):
                if lot.expiration_date is None or lot.expiration_date >= cutoff:
                    continue
                if lot.settlement_type == SettlementType.cash:
                    log.warning(
                        "lot %s (%s) is cash-settled but expired with no settlement "
                        "transaction; closing at 0 — verify it finished OTM",
                        lot.broker_txn_id, lot.symbol,
                    )
                event = PositionEvent(
                    txn_id=lot.broker_txn_id,  # unused; row gets a NULL broker id below
                    account=lot.account_id,
                    symbol=lot.symbol,
                    underlying=lot.underlying_symbol,
                    asset_type=lot.asset_type,
                    instrument_type="",
                    ts=datetime.combine(lot.expiration_date, time(21, 0)),
                    tx_date=lot.expiration_date,
                    qty=lot.remaining_quantity,
                    price=ZERO,
                    fees=ZERO,
                    kind=CLOSE,
                    direction=None,
                    close_reason=CloseReason.expiration,
                )
                row = self._record_close(event, lot, lot.side, lot.remaining_quantity)
                row.broker_close_txn_id = None  # no broker transaction exists
                lot.remaining_quantity = ZERO
                lots.remove(lot)
                swept += 1
        return swept


def rebuild_lots(
    session: Session,
    meta: MetaProvider,
    method: str = "fifo",
    grace_days: int = 4,
) -> dict:
    """Wipe and rebuild open_lots/lot_closes from raw_transactions."""
    session.execute(delete(LotClose))
    session.execute(delete(Lot))

    txns = (
        session.execute(
            select(RawTransaction).order_by(RawTransaction.executed_at, RawTransaction.id)
        )
        .scalars()
        .all()
    )
    events = classify_all(txns)

    matcher = Matcher(session, meta, method=method)
    for event in events:
        matcher.apply(event)
    matcher.finalize_links()

    # Sweep relative to the freshest data we have, never past "today": if the
    # DB is stale we can't distinguish "expired worthless" from "settlement
    # transaction not yet synced".
    max_seen = session.execute(select(func.max(RawTransaction.executed_at))).scalar_one_or_none()
    today = datetime.now(timezone.utc).date()
    as_of = min(today, max_seen.date()) if max_seen else today
    swept = matcher.expire_worthless(as_of, grace_days=grace_days)

    # Naming + roll-chain passes. Strategy names first (per opening order);
    # then link orders whose id closed old lots AND opened new ones
    # (chains.assign_chains). Runs after the sweep so settlement closes inherit
    # their lot's chain too.
    session.flush()
    all_lots = session.execute(select(Lot)).scalars().all()
    all_closes = session.execute(select(LotClose)).scalars().all()
    assign_strategy_names(all_lots, all_closes)
    n_chains = assign_chains(all_lots, all_closes)

    session.commit()

    open_count = session.execute(
        select(func.count()).select_from(Lot).where(Lot.remaining_quantity > 0)
    ).scalar_one()
    close_count = session.execute(select(func.count()).select_from(LotClose)).scalar_one()
    return {
        "transactions": len(txns),
        "events": len(events),
        "open_lots": open_count,
        "closes": close_count,
        "expired_worthless": swept,
        "chains": n_chains,
    }
