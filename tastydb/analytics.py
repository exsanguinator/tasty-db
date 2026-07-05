"""Realized-PnL queries over lot_closes.

realized_pnl is computed at match time as
    (close_price - open_price) * quantity_closed * multiplier * side_sign - fees
so these queries only filter and aggregate.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .marks import unrealized_pnl
from .models import AssetType, Lot, LotClose, Mark, Side


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


def _close_filters(stmt, start, end, underlying, account):
    if start is not None:
        stmt = stmt.where(LotClose.close_date >= datetime.combine(start, time.min))
    if end is not None:
        stmt = stmt.where(LotClose.close_date < datetime.combine(end + timedelta(days=1), time.min))
    if underlying is not None:
        stmt = stmt.where(LotClose.underlying_symbol == underlying)
    if account is not None:
        stmt = stmt.where(LotClose.account_id == account)
    return stmt


def list_closes(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    underlying: str | None = None,
    account: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[LotClose], int]:
    """Close rows for the browse view, newest first, plus the unpaged count."""
    base = _close_filters(select(LotClose), start, end, underlying, account)
    total = session.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    rows = session.execute(
        base.order_by(LotClose.close_date.desc(), LotClose.close_id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    return rows, total


def realized_timeseries(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    underlying: str | None = None,
    account: str | None = None,
) -> list[tuple[date, Decimal, Decimal]]:
    """(day, day PnL, cumulative PnL) points for the overview chart."""
    stmt = _close_filters(
        select(
            func.date(LotClose.close_date),
            func.sum(LotClose.realized_pnl),
        ),
        start, end, underlying, account,
    ).group_by(func.date(LotClose.close_date)).order_by(func.date(LotClose.close_date))
    points: list[tuple[date, Decimal, Decimal]] = []
    running = Decimal("0")
    for day_str, pnl in session.execute(stmt):
        day_pnl = Decimal(str(pnl or 0))
        running += day_pnl
        points.append((date.fromisoformat(str(day_str)), day_pnl, running))
    return points


@dataclass
class PositionRow:
    account_id: str
    symbol: str
    underlying_symbol: str
    asset_type: AssetType
    side: Side
    quantity: Decimal
    avg_open_price: Decimal
    cost_basis: Decimal  # |price| * qty * multiplier
    open_fees: Decimal  # remaining pro-rata share
    multiplier: Decimal
    expiration_date: date | None
    lot_ids: list[int] = field(default_factory=list)
    mark: Decimal | None = None
    mark_updated_at: datetime | None = None
    unrealized_pnl: Decimal | None = None


def open_positions(session: Session, account: str | None = None) -> list[PositionRow]:
    """Open lots aggregated per (account, symbol, side), joined to cached
    marks for gross unrealized PnL (None until marks are refreshed)."""
    stmt = select(Lot).where(Lot.remaining_quantity > 0)
    if account is not None:
        stmt = stmt.where(Lot.account_id == account)
    lots = session.execute(stmt.order_by(Lot.open_date)).scalars().all()
    marks = {m.symbol: m for m in session.execute(select(Mark)).scalars()}

    grouped: dict[tuple[str, str, Side], list[Lot]] = defaultdict(list)
    for lot in lots:
        grouped[(lot.account_id, lot.symbol, lot.side)].append(lot)

    q4 = Decimal("0.0001")
    rows: list[PositionRow] = []
    for (account_id, symbol, side), group in grouped.items():
        qty = sum((l.remaining_quantity for l in group), Decimal("0"))
        avg_price = (
            sum((l.open_price * l.remaining_quantity for l in group), Decimal("0")) / qty
        ).quantize(Decimal("0.00000001"))
        fees = sum(
            (
                (l.open_fees * l.remaining_quantity / l.original_quantity).quantize(q4)
                for l in group if l.original_quantity
            ),
            Decimal("0"),
        )
        first = group[0]
        row = PositionRow(
            account_id=account_id,
            symbol=symbol,
            underlying_symbol=first.underlying_symbol,
            asset_type=first.asset_type,
            side=side,
            quantity=qty,
            avg_open_price=avg_price,
            cost_basis=(avg_price * qty * first.multiplier).quantize(q4),
            open_fees=fees,
            multiplier=first.multiplier,
            expiration_date=first.expiration_date,
            lot_ids=[l.lot_id for l in group],
        )
        mark = marks.get(symbol)
        if mark is not None and mark.mark is not None:
            row.mark = mark.mark
            row.mark_updated_at = mark.updated_at
            row.unrealized_pnl = unrealized_pnl(
                avg_price, mark.mark, qty, first.multiplier, side
            )
        rows.append(row)
    rows.sort(key=lambda r: (r.account_id, r.underlying_symbol, r.symbol))
    return rows


@dataclass
class StrategyLeg:
    symbol: str
    side: Side
    quantity: Decimal
    realized_pnl: Decimal


@dataclass
class StrategyRow:
    open_order_id: int | None
    account_id: str
    underlying_symbol: str
    open_date: datetime
    close_date: datetime
    legs: list[StrategyLeg]
    closes: int
    realized_pnl: Decimal
    close_reasons: list[str]
    lot_ids: list[int] = field(default_factory=list)
    chain_id: int | None = None  # roll chain this strategy belongs to, if any


def strategies(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    underlying: str | None = None,
    account: str | None = None,
    limit: int = 100,
) -> list[StrategyRow]:
    """Realized closes grouped by the opening order id — the legs of a spread
    entered as one order form one strategy. Closes without an opening order
    (deliveries, history gaps) group per lot instead."""
    stmt = _close_filters(select(LotClose), start, end, underlying, account)
    closes = session.execute(stmt).scalars().all()

    grouped: dict[object, list[LotClose]] = defaultdict(list)
    for close in closes:
        key = close.open_order_id if close.open_order_id is not None else f"lot:{close.lot_id}"
        grouped[key].append(close)

    rows: list[StrategyRow] = []
    for key, group in grouped.items():
        legs: dict[tuple[str, Side], StrategyLeg] = {}
        for c in group:
            leg = legs.get((c.symbol, c.side))
            if leg is None:
                legs[(c.symbol, c.side)] = StrategyLeg(
                    symbol=c.symbol, side=c.side,
                    quantity=c.quantity_closed, realized_pnl=c.realized_pnl,
                )
            else:
                leg.quantity += c.quantity_closed
                leg.realized_pnl += c.realized_pnl
        rows.append(StrategyRow(
            open_order_id=group[0].open_order_id,
            account_id=group[0].account_id,
            underlying_symbol=group[0].underlying_symbol,
            open_date=min(c.open_date for c in group),
            close_date=max(c.close_date for c in group),
            legs=sorted(legs.values(), key=lambda l: l.symbol),
            closes=len(group),
            realized_pnl=sum((c.realized_pnl for c in group), Decimal("0")),
            close_reasons=sorted({c.close_reason.value for c in group}),
            lot_ids=sorted({c.lot_id for c in group}),
            chain_id=next((c.chain_id for c in group if c.chain_id is not None), None),
        ))
    rows.sort(key=lambda r: r.close_date, reverse=True)
    return rows[:limit]
