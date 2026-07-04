"""Realized-PnL queries over lot_closes.

realized_pnl is computed at match time as
    (close_price - open_price) * quantity_closed * multiplier * side_sign - fees
so these queries only filter and aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import LotClose


@dataclass
class PnlRow:
    group: str
    closes: int
    quantity_closed: Decimal
    fees: Decimal
    realized_pnl: Decimal


def realized_pnl(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    underlying: str | None = None,
    account: str | None = None,
    group_by: str = "underlying",  # underlying | asset_type | close_reason
) -> list[PnlRow]:
    group_col = {
        "underlying": LotClose.underlying_symbol,
        "asset_type": LotClose.asset_type,
        "close_reason": LotClose.close_reason,
    }[group_by]

    stmt = select(
        group_col,
        func.count(LotClose.close_id),
        func.sum(LotClose.quantity_closed),
        func.sum(LotClose.open_fees + LotClose.close_fees),
        func.sum(LotClose.realized_pnl),
    )
    if start is not None:
        stmt = stmt.where(LotClose.close_date >= datetime.combine(start, time.min))
    if end is not None:
        # inclusive end date
        stmt = stmt.where(LotClose.close_date < datetime.combine(end + timedelta(days=1), time.min))
    if underlying is not None:
        stmt = stmt.where(LotClose.underlying_symbol == underlying)
    if account is not None:
        stmt = stmt.where(LotClose.account_id == account)
    stmt = stmt.group_by(group_col).order_by(func.sum(LotClose.realized_pnl).desc())

    def _dec(v) -> Decimal:
        return Decimal(str(v)) if v is not None else Decimal("0")

    return [
        PnlRow(
            group=getattr(g, "value", g),
            closes=n,
            quantity_closed=_dec(q),
            fees=_dec(f),
            realized_pnl=_dec(p),
        )
        for g, n, q, f, p in session.execute(stmt)
    ]
