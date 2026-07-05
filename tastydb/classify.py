"""Classification: raw transactions -> typed position events.

This is deliberately separate from ingest (requirement: matching bugs must not
require re-fetching) and from matching (so the rules are testable in isolation).

Rule summary, keyed off transaction-type / transaction-sub-type / action:

  Trade
    action "Buy to Open"/"Sell to Open"   -> OPEN
    action "Buy to Close"/"Sell to Close" -> CLOSE (reason=trade)
    action "Buy"/"Sell" (futures, plain)  -> NET (close opposite side first,
                                             remainder opens; handles crossing zero)
  Receive Deliver
    sub-type contains "cash settled"      -> CLOSE reason=cash_settlement.
        The transaction's `value` IS the exchange settlement cash (intrinsic at
        the official settlement value); we derive the effective close price
        from it rather than from any quote (the txn's `price` field is the
        strike, not a close price). Checked BEFORE the generic expiration
        path, so an ITM cash-settled expiry never books as worthless.
        Observed in real payloads: tastytrade posts cash settlement as TWO
        transactions per leg — "Cash Settled Exercise/Assignment" carrying the
        cash, then a plain "Exercise"/"Assignment" removal with value 0. The
        removal is skipped when a cash-settled sibling exists for the same
        symbol and date (see classify_all), otherwise it would double-close.
    sub-type "Expiration"                 -> CLOSE reason=expiration at price 0
    sub-type "Assignment"                 -> option removal: CLOSE short lots at 0
    sub-type "Exercise"                   -> option removal: CLOSE long lots at 0
    action present (delivery leg: the stock/future bought or sold at the
        strike as a result of assignment/exercise)
                                          -> NET, flagged as a delivery so the
                                             matcher can link it (linked_lot_id)
    splits / symbol changes / other       -> unsupported (flagged, never silent)
  everything else (Money Movement, ...)   -> ignored

Transactions carrying `reverses-id` cancel the referenced transaction; both
sides of a reversal pair are excluded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from .instruments import INSTRUMENT_TYPE_TO_ASSET
from .models import AssetType, CloseReason, ProcessingStatus, RawTransaction

log = logging.getLogger(__name__)

OPEN = "open"
CLOSE = "close"
NET = "net"

BUY = "buy"
SELL = "sell"

_ACTION_MAP = {
    "buy to open": (OPEN, BUY),
    "sell to open": (OPEN, SELL),
    "buy to close": (CLOSE, BUY),
    "sell to close": (CLOSE, SELL),
    "buy": (NET, BUY),
    "sell": (NET, SELL),
}

_OPTION_ASSET_TYPES = {AssetType.equity_option, AssetType.future_option}

# Receive Deliver sub-types that change positions in ways we don't model.
_UNSUPPORTED_SUBTYPES = {
    "forward split", "reverse split", "symbol change", "stock merger",
    "acat", "transfer", "dividend", "special dividend", "liquidation",
}


@dataclass
class PositionEvent:
    txn_id: int
    account: str
    symbol: str
    underlying: str
    asset_type: AssetType
    instrument_type: str
    ts: datetime
    tx_date: date
    qty: Decimal
    price: Decimal | None
    fees: Decimal
    kind: str  # OPEN | CLOSE | NET
    direction: str | None  # BUY | SELL | None (None = close whichever side is open)
    gross_value: Decimal | None = None  # unsigned txn value (= price*qty*multiplier for options)
    order_id: int | None = None  # broker order id (None on Receive Deliver txns)
    close_reason: CloseReason = CloseReason.trade
    cash_value: Decimal | None = None  # signed settlement cash (credit positive)
    is_delivery: bool = False  # assignment/exercise delivery leg -> link candidate
    is_removal: bool = False  # assignment/exercise option removal -> link source

    @property
    def link_key(self) -> tuple[str, str, date]:
        return (self.account, self.underlying, self.tx_date)


def _base_kwargs(txn: RawTransaction, asset_type: AssetType) -> dict:
    return dict(
        txn_id=txn.id,
        account=txn.account_number,
        symbol=txn.symbol,
        underlying=txn.underlying_symbol or txn.symbol,
        asset_type=asset_type,
        instrument_type=txn.instrument_type or "",
        ts=txn.executed_at,
        tx_date=txn.transaction_date or txn.executed_at.date(),
        qty=txn.quantity or Decimal("0"),
        price=txn.price,
        fees=txn.total_fees or Decimal("0"),
        gross_value=abs(txn.value) if txn.value is not None else None,
        order_id=(txn.payload or {}).get("order-id"),
    )


def _signed_value(txn: RawTransaction) -> Decimal:
    """Transaction gross value signed so that credits are positive."""
    value = txn.value or Decimal("0")
    return -value if txn.value_effect == "Debit" else value


def classify_transaction(
    txn: RawTransaction,
    cash_settled_keys: frozenset[tuple[str, date]] = frozenset(),
) -> tuple[PositionEvent | None, ProcessingStatus, str | None]:
    """Return (event, status, note) for one transaction. Never raises on
    unexpected type strings — unknown position-affecting shapes come back as
    `unsupported` so they surface in `tastydb status` instead of vanishing.

    cash_settled_keys holds (symbol, transaction_date) of every cash-settled
    transaction in the batch, used to drop the redundant removal legs."""
    ttype = (txn.transaction_type or "").strip().lower()
    subtype = (txn.transaction_sub_type or "").strip().lower()
    action = (txn.action or "").strip().lower()

    asset_type = INSTRUMENT_TYPE_TO_ASSET.get(txn.instrument_type or "")
    if ttype not in ("trade", "receive deliver"):
        return None, ProcessingStatus.ignored, f"transaction-type={txn.transaction_type!r}"
    if asset_type is None or not txn.symbol:
        return None, ProcessingStatus.ignored, f"instrument-type={txn.instrument_type!r}"
    if not txn.quantity:
        return None, ProcessingStatus.ignored, "zero/absent quantity"

    if ttype == "trade":
        mapped = _ACTION_MAP.get(action)
        if mapped is None:
            return None, ProcessingStatus.unsupported, f"unknown Trade action {txn.action!r}"
        kind, direction = mapped
        return (
            PositionEvent(**_base_kwargs(txn, asset_type), kind=kind, direction=direction),
            ProcessingStatus.processed,
            None,
        )

    # -- Receive Deliver ----------------------------------------------------

    if "cash settled" in subtype:
        # Cash-settled expiry/exercise/assignment. Direction: exercises remove
        # long lots, assignments remove short lots.
        direction = SELL if "exercise" in subtype else BUY if "assignment" in subtype else None
        return (
            PositionEvent(
                **_base_kwargs(txn, asset_type),
                kind=CLOSE,
                direction=direction,
                close_reason=CloseReason.cash_settlement,
                cash_value=_signed_value(txn),
            ),
            ProcessingStatus.processed,
            None,
        )

    if subtype == "expiration":
        kwargs = _base_kwargs(txn, asset_type)
        kwargs["price"] = Decimal("0")
        return (
            PositionEvent(
                **kwargs,
                kind=CLOSE,
                direction=None,  # worthless expiry removes whichever side is open
                close_reason=CloseReason.expiration,
            ),
            ProcessingStatus.processed,
            None,
        )

    if subtype in ("assignment", "exercise") and asset_type in _OPTION_ASSET_TYPES:
        tx_date = txn.transaction_date or txn.executed_at.date()
        if (txn.symbol, tx_date) in cash_settled_keys:
            # Redundant removal leg of a cash settlement; the cash-settled
            # sibling transaction already closed the lot at the settlement value.
            return None, ProcessingStatus.ignored, "removal superseded by cash settlement"
        # Option removal leg: the contract disappears at price 0; economics of
        # the strike land on the delivery leg's lot basis.
        reason = CloseReason.assignment if subtype == "assignment" else CloseReason.exercise
        kwargs = _base_kwargs(txn, asset_type)
        kwargs["price"] = Decimal("0")
        return (
            PositionEvent(
                **kwargs,
                kind=CLOSE,
                direction=BUY if subtype == "assignment" else SELL,
                close_reason=reason,
                is_removal=True,
            ),
            ProcessingStatus.processed,
            None,
        )

    if action in _ACTION_MAP:
        # Delivery leg: stock/future received or delivered at the strike.
        # NET (not the literal action) because delivery may open a new lot,
        # close an existing one, or both.
        _, direction = _ACTION_MAP[action]
        return (
            PositionEvent(
                **_base_kwargs(txn, asset_type),
                kind=NET,
                direction=direction,
                is_delivery=True,
            ),
            ProcessingStatus.processed,
            None,
        )

    if subtype in _UNSUPPORTED_SUBTYPES:
        return None, ProcessingStatus.unsupported, f"Receive Deliver sub-type {txn.transaction_sub_type!r}"
    return None, ProcessingStatus.unsupported, f"unrecognized Receive Deliver {txn.transaction_sub_type!r}/{txn.action!r}"


def classify_all(txns: list[RawTransaction]) -> list[PositionEvent]:
    """Classify transactions (already sorted by executed_at, id), updating each
    row's processing_status in place. Reversal pairs are excluded first."""
    reversed_ids: set[int] = set()
    cash_settled_keys: set[tuple[str, date]] = set()
    for txn in txns:
        rid = (txn.payload or {}).get("reverses-id")
        if rid:
            reversed_ids.add(int(rid))
            reversed_ids.add(txn.id)
        if txn.symbol and "cash settled" in (txn.transaction_sub_type or "").lower():
            cash_settled_keys.add(
                (txn.symbol, txn.transaction_date or txn.executed_at.date())
            )
    frozen_keys = frozenset(cash_settled_keys)

    events: list[PositionEvent] = []
    for txn in txns:
        if txn.id in reversed_ids:
            txn.processing_status = ProcessingStatus.reversed
            txn.processing_note = "excluded: part of a reversal pair"
            continue
        try:
            event, status, note = classify_transaction(txn, frozen_keys)
        except Exception as exc:  # noqa: BLE001 - one bad row must not kill the run
            log.exception("classification failed for txn %s", txn.id)
            txn.processing_status = ProcessingStatus.error
            txn.processing_note = f"{type(exc).__name__}: {exc}"
            continue
        txn.processing_status = status
        txn.processing_note = note
        if event is not None:
            events.append(event)
    return events
