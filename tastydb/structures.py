"""Friendly names for multi-leg option structures ("Iron condor", "Strangle").

TastyTrade's API does not expose a strategy name anywhere: order objects carry
`leg-count` and prices but no structure field, `complex-order-id`/-tag describe
OTO/OCO linkage rather than leg shape, and transaction payloads have nothing
either. The broker's own "Order Chain" names are computed client-side, so we
derive ours the same way — from the shape of the legs entered together.

`name_structure` is pure: it takes `LegShape`s and returns a label, falling
back to "Custom" for anything unrecognized (same fallback the broker's UI
uses). `assign_strategy_names` is the one place that name reaches the DB —
`rebuild_lots` calls it to stamp `strategy_name` on every lot and close, so
reports can aggregate by strategy in SQL.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Sequence

from .models import AssetType, OptionType, Side

if TYPE_CHECKING:
    from .models import Lot, LotClose

CUSTOM = "Custom"


@dataclass(frozen=True)
class LegShape:
    """One leg of an order, reduced to what naming depends on."""
    side: Side
    option_type: OptionType | None  # None = stock / outright future
    strike: Decimal | None
    expiration: date | None
    quantity: Decimal
    asset_type: AssetType | None = None


def _single(leg: LegShape) -> str:
    what = leg.option_type.value if leg.option_type else (
        "future" if leg.asset_type in (AssetType.future, AssetType.future_option) else "stock"
    )
    return f"{leg.side.value.capitalize()} {what}"


def _vertical(short: LegShape, long_: LegShape) -> str:
    # A short leg closer to the money than the long one collects net premium.
    kind = short.option_type
    credit = (
        short.strike < long_.strike if kind is OptionType.call
        else short.strike > long_.strike
    )
    return f"{kind.value.capitalize()} {'credit' if credit else 'debit'} spread"


def _two_options(legs: Sequence[LegShape]) -> str:
    a, b = legs
    same_expiry = a.expiration == b.expiration
    same_strike = a.strike == b.strike

    if a.option_type is b.option_type:
        if not same_expiry:
            return "Calendar" if same_strike else "Diagonal"
        if a.side is b.side:
            return CUSTOM
        short, long_ = (a, b) if a.side is Side.short else (b, a)
        if a.quantity != b.quantity:
            return f"{a.option_type.value.capitalize()} ratio spread"
        return _vertical(short, long_)

    # One call, one put.
    if same_expiry:
        if a.side is b.side:
            shape = "straddle" if same_strike else "strangle"
            return f"{a.side.value.capitalize()} {shape}"
        call = a if a.option_type is OptionType.call else b
        # Synthetic stock: long the upside and short the downside, or vice versa.
        return "Superbull" if call.side is Side.long else "Superbear"
    return CUSTOM


def _stock_plus_option(stock: LegShape, option: LegShape) -> str:
    if stock.side is Side.long and option.option_type is OptionType.call \
            and option.side is Side.short:
        return "Covered call"
    if stock.side is Side.long and option.option_type is OptionType.put \
            and option.side is Side.long:
        return "Protective put"
    return CUSTOM


def _butterfly(legs: Sequence[LegShape]) -> str:
    """Three same-type legs at three strikes in 1-2-1 proportion: long wings
    around a double-quantity short body (or the reverse)."""
    by_strike = sorted(legs, key=lambda l: l.strike)
    low, mid, high = by_strike
    if low.side is not high.side or mid.side is low.side:
        return CUSTOM
    if low.quantity != high.quantity or mid.quantity != low.quantity * 2:
        return CUSTOM
    kind = low.option_type.value.capitalize()
    balanced = mid.strike - low.strike == high.strike - mid.strike
    return f"{kind} butterfly" if balanced else f"Broken-wing {kind.lower()} butterfly"


def _iron(legs: Sequence[LegShape]) -> str:
    calls = [l for l in legs if l.option_type is OptionType.call]
    puts = [l for l in legs if l.option_type is OptionType.put]
    if len(calls) != 2 or len(puts) != 2:
        return CUSTOM
    if {l.side for l in calls} != {Side.long, Side.short}:
        return CUSTOM
    if {l.side for l in puts} != {Side.long, Side.short}:
        return CUSTOM
    short_call = next(l for l in calls if l.side is Side.short)
    short_put = next(l for l in puts if l.side is Side.short)
    return "Iron fly" if short_call.strike == short_put.strike else "Iron condor"


def name_structure(legs: Sequence[LegShape]) -> str:
    """Name the structure these legs form, or "Custom" if nothing fits."""
    if not legs:
        return CUSTOM
    if len(legs) == 1:
        return _single(legs[0])

    options = [l for l in legs if l.option_type is not None]
    others = [l for l in legs if l.option_type is None]

    if len(legs) == 2:
        if len(options) == 2:
            return _two_options(options)
        if len(options) == 1:
            return _stock_plus_option(others[0], options[0])
        return CUSTOM

    if others:  # structures below are options-only
        return CUSTOM
    if len({l.expiration for l in options}) != 1:
        return CUSTOM

    if len(legs) == 3 and len({l.option_type for l in options}) == 1:
        if len({l.strike for l in options}) == 3:
            return _butterfly(options)
        return CUSTOM
    if len(legs) == 4:
        return _iron(options)
    return CUSTOM


# -- stamping (called from matching.rebuild_lots) ------------------------------


def assign_strategy_names(lots: list["Lot"], closes: list["LotClose"]) -> int:
    """Stamp `strategy_name` on lots and closes; returns the number of groups.

    Grouping key is (account, open_order_id) — the legs of one order form one
    structure. Lots with no opening order (assignment deliveries, sweeps) are
    named individually. Closes inherit from their lot, exactly as chain_id does.
    Unlike the strategies view this needs no symbol parsing: lots store
    strike/expiry/right as columns.
    """
    groups: dict[object, list] = defaultdict(list)
    for lot in lots:
        key = ((lot.account_id, lot.open_order_id) if lot.open_order_id is not None
               else ("lot", lot.lot_id))
        groups[key].append(lot)

    for group in groups.values():
        by_symbol: dict[str, LegShape] = {}
        for lot in group:
            leg = by_symbol.get(lot.symbol)
            if leg is None:
                by_symbol[lot.symbol] = LegShape(
                    side=lot.side, option_type=lot.option_type, strike=lot.strike,
                    expiration=lot.expiration_date, quantity=lot.original_quantity,
                    asset_type=lot.asset_type,
                )
            else:
                by_symbol[lot.symbol] = replace(
                    leg, quantity=leg.quantity + lot.original_quantity
                )
        name = name_structure(list(by_symbol.values()))
        for lot in group:
            lot.strategy_name = name

    lot_names = {lot.lot_id: lot.strategy_name for lot in lots}
    for close in closes:
        close.strategy_name = lot_names.get(close.lot_id)
    return len(groups)
