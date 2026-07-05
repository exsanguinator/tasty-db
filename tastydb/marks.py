"""Mark cache: latest market snapshots for open-lot symbols, powering
unrealized PnL. One row per symbol in `marks`, overwritten on refresh."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .client import TastyClient
from .models import AssetType, Lot, Mark, Side

log = logging.getLogger(__name__)

# asset_type -> /market-data/by-type query param name
_MARKET_DATA_PARAM = {
    AssetType.stock: "equity",
    AssetType.equity_option: "equity-option",
    AssetType.future: "future",
    AssetType.future_option: "future-option",
}


def open_symbols_by_type(session: Session) -> dict[str, list[str]]:
    rows = session.execute(
        select(Lot.symbol, Lot.asset_type)
        .where(Lot.remaining_quantity > 0)
        .distinct()
    ).all()
    grouped: dict[str, list[str]] = {}
    for symbol, asset_type in rows:
        grouped.setdefault(_MARKET_DATA_PARAM[asset_type], []).append(symbol)
    return grouped


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def refresh_marks(session: Session, client: TastyClient) -> int:
    """Fetch snapshots for every open-lot symbol and upsert the marks table.
    Returns the number of symbols updated."""
    grouped = open_symbols_by_type(session)
    if not grouped:
        return 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    updated = 0
    for item in client.market_data_by_type(grouped):
        symbol = item.get("symbol")
        if not symbol:
            continue
        row = session.get(Mark, symbol) or Mark(symbol=symbol)
        row.instrument_type = item.get("instrumentType")
        row.mark = _dec(item.get("mark")) or _dec(item.get("mid")) or _dec(item.get("last"))
        row.bid = _dec(item.get("bid"))
        row.ask = _dec(item.get("ask"))
        row.mid = _dec(item.get("mid"))
        row.updated_at = now
        session.add(row)
        updated += 1
    session.commit()
    return updated


def unrealized_pnl(
    open_price: Decimal, mark: Decimal, quantity: Decimal, multiplier: Decimal, side: Side
) -> Decimal:
    """Gross unrealized PnL (fees not netted; they're shown separately)."""
    sign = 1 if side == Side.long else -1
    return ((mark - open_price) * quantity * multiplier * sign).quantize(Decimal("0.0001"))
