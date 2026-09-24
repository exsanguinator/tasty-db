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

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .marks import unrealized_pnl
from .models import AssetType, Lot, LotClose, Mark, ProcessingStatus, RawTransaction, Side
from .structures import CUSTOM

# strategy group labels for rows the derived tables don't name
UNNAMED = "Unnamed"
UNMATCHED = "Unmatched"

_CREDIT_ACTIONS = (
    "buy to open", "sell to open", "buy to close", "sell to close", "buy", "sell",
)


def _strategy_where(strategy: str):
    """UNNAMED is the label a NULL strategy_name is coalesced to, so selecting
    it has to look for the NULL rather than for the label itself."""
    if strategy == UNNAMED:
        return LotClose.strategy_name.is_(None)
    return LotClose.strategy_name == strategy


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
    strategy: str | None = None,
    group_by: str = "underlying",  # underlying | asset_type | close_reason | strategy
) -> list[PnlRow]:
    group_col = {
        "underlying": LotClose.underlying_symbol,
        "asset_type": LotClose.asset_type,
        "close_reason": LotClose.close_reason,
        # NULL only for closes whose lot predates a rebuild, but coalesce keeps
        # them a visible row rather than a silently dropped SQL NULL group.
        "strategy": func.coalesce(LotClose.strategy_name, UNNAMED),
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
    if strategy is not None:
        stmt = stmt.where(_strategy_where(strategy))
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


@dataclass
class CreditRow:
    group: str  # underlying symbol, or strategy name when grouped by strategy
    trades: int
    credits: Decimal


def _credit_signed_value():
    return case(
        (RawTransaction.value_effect == "Debit", -RawTransaction.value),
        else_=RawTransaction.value,
    )


def _is_mark_to_market():
    return (
        (func.lower(RawTransaction.transaction_type) == "money movement")
        & (func.lower(RawTransaction.transaction_sub_type) == "mark to market")
    )


def _credit_filters(stmt, start, end, underlying, account):
    """Restrict to credit-bearing rows (see `credits_collected`)."""
    stmt = stmt.where(
        RawTransaction.processing_status.notin_(
            (ProcessingStatus.reversed, ProcessingStatus.error)
        ),
        (
            func.lower(RawTransaction.transaction_type).in_(("trade", "receive deliver"))
            & (
                func.lower(RawTransaction.action).in_(_CREDIT_ACTIONS)
                | func.lower(RawTransaction.transaction_sub_type).contains("cash settled")
            )
        )
        # A futures trade's `value` only settles against the previous daily
        # mark; the rest of the position's cash moves in these daily rows.
        | _is_mark_to_market(),
    )
    if start is not None:
        stmt = stmt.where(RawTransaction.executed_at >= datetime.combine(start, time.min))
    if end is not None:
        stmt = stmt.where(RawTransaction.executed_at < datetime.combine(end + timedelta(days=1), time.min))
    if underlying is not None:
        stmt = stmt.where(RawTransaction.underlying_symbol == underlying)
    if account is not None:
        stmt = stmt.where(RawTransaction.account_number == account)
    return stmt


def _strategy_of_transaction(session: Session) -> dict[int, str]:
    """Broker transaction id -> strategy name, via the derived tables: a lot's
    id IS its opening transaction, and lot_closes records the closing one.

    Deliberately not a SQL join on lot_closes: one closing transaction can
    close several lots, which would multiply that transaction's value across
    the join and inflate the credits. Resolving to one name per transaction
    first keeps every transaction counted exactly once. Ties (a close spanning
    lots of different strategies) go to the lowest lot id, for determinism."""
    names: dict[int, str] = {}
    for txn_id, name in session.execute(
        select(Lot.lot_id, Lot.strategy_name)
    ):
        names[txn_id] = name or CUSTOM
    for txn_id, name in session.execute(
        select(LotClose.broker_close_txn_id, LotClose.strategy_name)
        .where(LotClose.broker_close_txn_id.is_not(None))
        .order_by(LotClose.lot_id)
    ):
        names.setdefault(txn_id, name or CUSTOM)
    return names


def _mtm_strategy_resolver(session: Session):
    """(account, symbol, settled_at) -> strategy of the futures lot held
    through that daily mark-to-market. MTM rows belong to no lot, so they are
    attributed to the earliest lot open at the mark. A lot closed earlier the
    same day no longer marks, but one closed AT the settlement instant (a
    futures-option delivery stamped at the close) still does."""
    last_close = (
        select(LotClose.lot_id, func.max(LotClose.close_date).label("closed_at"))
        .group_by(LotClose.lot_id)
        .subquery()
    )
    held: dict[tuple[str, str], list[tuple[datetime, datetime | None, str]]] = defaultdict(list)
    for account, symbol, opened, remaining, closed, name in session.execute(
        select(
            Lot.account_id, Lot.symbol, Lot.open_date, Lot.remaining_quantity,
            last_close.c.closed_at, Lot.strategy_name,
        )
        .outerjoin(last_close, last_close.c.lot_id == Lot.lot_id)
        .where(Lot.asset_type == AssetType.future)
        .order_by(Lot.open_date, Lot.lot_id)
    ):
        until = None if remaining > 0 else closed
        held[(account, symbol)].append((opened, until, name or CUSTOM))

    def resolve(account: str, symbol: str, settled_at: datetime) -> str:
        for opened, until, name in held.get((account, symbol), ()):
            if opened <= settled_at and (until is None or until >= settled_at):
                return name
        return UNMATCHED

    return resolve


@dataclass
class CreditTxnRow:
    """One credit-bearing raw transaction, with the strategy it belongs to."""

    txn_id: int
    date: date | None  # broker transaction_date (the cash-flow convention)
    executed_at: datetime
    symbol: str | None
    underlying_symbol: str | None
    action: str | None
    quantity: Decimal | None
    credits: Decimal  # signed: positive = collected, negative = paid
    strategy: str


def _credit_txns(session, start, end, underlying, account) -> list[CreditTxnRow]:
    """Every credit-bearing transaction in range, each resolved to exactly one
    strategy name (UNMATCHED when no lot claims it)."""
    stmt = _credit_filters(
        select(
            RawTransaction.id,
            RawTransaction.account_number,
            RawTransaction.transaction_date,
            RawTransaction.executed_at,
            RawTransaction.symbol,
            RawTransaction.underlying_symbol,
            RawTransaction.action,
            RawTransaction.transaction_sub_type,
            RawTransaction.quantity,
            _credit_signed_value(),
            _is_mark_to_market(),
        ),
        start, end, underlying, account,
    )
    names = _strategy_of_transaction(session)
    mtm_strategy = None
    rows = []
    for (
        txn_id, account_number, txn_date, executed_at, symbol, underlying_symbol,
        action, sub_type, quantity, value, is_mtm,
    ) in session.execute(stmt):
        if is_mtm:
            mtm_strategy = mtm_strategy or _mtm_strategy_resolver(session)
            strategy = mtm_strategy(account_number, symbol, executed_at)
        else:
            strategy = names.get(txn_id, UNMATCHED)
        rows.append(CreditTxnRow(
            txn_id=txn_id,
            date=txn_date,
            executed_at=executed_at,
            symbol=symbol,
            underlying_symbol=underlying_symbol,
            action=action or sub_type,  # MTM rows have no action
            quantity=Decimal(str(quantity)) if quantity is not None else None,
            credits=Decimal(str(value or 0)),
            strategy=strategy,
        ))
    return rows


def _credits_by_strategy(session, start, end, underlying, account) -> list[CreditRow]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    trades: dict[str, int] = defaultdict(int)
    for txn in _credit_txns(session, start, end, underlying, account):
        totals[txn.strategy] += txn.credits
        trades[txn.strategy] += 1
    rows = [CreditRow(group=g, trades=trades[g], credits=totals[g]) for g in totals]
    rows.sort(key=lambda r: r.credits, reverse=True)
    return rows


def list_credit_transactions(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    underlying: str | None = None,
    account: str | None = None,
    strategy: str | None = None,
    limit: int = 200,
) -> tuple[list[CreditTxnRow], int, Decimal]:
    """The individual transactions behind a `credits_collected` row: the shown
    slice (newest first), the unpaged count, and the total over all of them."""
    rows = _credit_txns(session, start, end, underlying, account)
    if strategy is not None:
        rows = [r for r in rows if r.strategy == strategy]
    total = sum((r.credits for r in rows), Decimal("0"))
    rows.sort(key=lambda r: (r.executed_at, r.txn_id), reverse=True)
    return rows[:limit], len(rows), total


def credits_collected(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    underlying: str | None = None,
    account: str | None = None,
    group_by: str = "underlying",  # underlying | strategy
) -> list[CreditRow]:
    """Cash collected selling minus cash paid buying, from raw trade actions
    (Trade rows, assignment/exercise delivery legs, and cash-settled
    exercise/assignment rows) plus futures daily mark-to-market settlements,
    grouped by underlying or by strategy. Gross of fees.

    Futures need the MTM rows: a futures trade's broker `value` is only the
    cash since the previous daily settlement (opens are 0), so trades alone
    miss most of the position's cash. With them, a flat futures position's
    credits equal its gross realized PnL."""
    if group_by == "strategy":
        return _credits_by_strategy(session, start, end, underlying, account)
    signed_value = _credit_signed_value()
    stmt = _credit_filters(
        select(
            RawTransaction.underlying_symbol,
            func.count(),
            func.sum(signed_value),
        ),
        start, end, underlying, account,
    ).group_by(RawTransaction.underlying_symbol).order_by(func.sum(signed_value).desc())

    return [
        CreditRow(group=group, trades=n, credits=Decimal(str(total)) if total is not None else Decimal("0"))
        for group, n, total in session.execute(stmt)
    ]


def credits_timeseries(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    underlying: str | None = None,
    account: str | None = None,
) -> list[tuple[date, Decimal, Decimal]]:
    """(day, day credits, cumulative credits) points for the credits chart."""
    day = func.date(RawTransaction.executed_at)
    stmt = _credit_filters(
        select(day, func.sum(_credit_signed_value())),
        start, end, underlying, account,
    ).group_by(day).order_by(day)
    points: list[tuple[date, Decimal, Decimal]] = []
    running = Decimal("0")
    for day_str, total in session.execute(stmt):
        day_credits = Decimal(str(total or 0))
        running += day_credits
        points.append((date.fromisoformat(str(day_str)), day_credits, running))
    return points


def _close_filters(stmt, start, end, underlying, account, strategy=None):
    if start is not None:
        stmt = stmt.where(LotClose.close_date >= datetime.combine(start, time.min))
    if end is not None:
        stmt = stmt.where(LotClose.close_date < datetime.combine(end + timedelta(days=1), time.min))
    if underlying is not None:
        stmt = stmt.where(LotClose.underlying_symbol == underlying)
    if account is not None:
        stmt = stmt.where(LotClose.account_id == account)
    if strategy is not None:
        stmt = stmt.where(_strategy_where(strategy))
    return stmt


def list_closes(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    underlying: str | None = None,
    account: str | None = None,
    strategy: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[LotClose], int]:
    """Close rows for the browse view, newest first, plus the unpaged count."""
    base = _close_filters(select(LotClose), start, end, underlying, account, strategy)
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
    strategy: str | None = None,
) -> list[tuple[date, Decimal, Decimal]]:
    """(day, day PnL, cumulative PnL) points for the overview chart."""
    stmt = _close_filters(
        select(
            func.date(LotClose.close_date),
            func.sum(LotClose.realized_pnl),
        ),
        start, end, underlying, account, strategy,
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
    strategy_name: str = CUSTOM  # derived leg shape, e.g. "Iron condor"


def strategies(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    underlying: str | None = None,
    account: str | None = None,
    strategy: str | None = None,
    limit: int = 100,
) -> list[StrategyRow]:
    """Realized closes grouped by the opening order id — the legs of a spread
    entered as one order form one strategy. Closes without an opening order
    (deliveries, history gaps) group per lot instead."""
    stmt = _close_filters(select(LotClose), start, end, underlying, account, strategy)
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
            strategy_name=group[0].strategy_name or CUSTOM,
        ))
    rows.sort(key=lambda r: r.close_date, reverse=True)
    return rows[:limit]
