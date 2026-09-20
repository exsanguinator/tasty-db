"""Roll chains: orders linked by rolls, grouped into campaigns.

A roll order closes the old contract and opens the new one in a single order,
so its broker order id appears both as `close_order_id` on the old lots'
closes and as `open_order_id` on the lots it opened — that shared id is the
chain link. `assign_chains` walks those links transitively (union-find) and
stamps `chain_id` on every lot/close of a linked campaign. chain_id is the
root (earliest) opening order id, so it is deterministic across rebuilds.

Rules:

- Chains are keyed per (account, underlying): one order carrying legs of two
  underlyings (a pairs trade) never cross-links two campaigns.
- Only genuine chains are stamped (two or more linked orders); a plain
  open-then-close keeps chain_id NULL and appears under strategies only.
- Chains merge naturally: one order rolling two separate positions joins
  their histories, and partial rolls link too — any shared order id links.
- Sweep/settlement closes carry no order id; they inherit the chain through
  their lot, so a campaign that ends in a worthless expiration stays whole.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Lot, LotClose, Side
from .structures import CUSTOM, LegShape, name_structure

ZERO = Decimal("0")
Q_MONEY = Decimal("0.0001")


# -- chain assignment (called from matching.rebuild_lots) ---------------------


def assign_chains(lots: list[Lot], closes: list[LotClose]) -> int:
    """Stamp chain_id on lots and closes; returns the number of chains found."""
    # each (account, underlying, opening-order) is a union-find node; remember
    # its earliest open so the chain root is the campaign's first order
    first_open: dict[tuple[str, str, int], datetime] = {}
    for lot in lots:
        if lot.open_order_id is None:
            continue
        node = (lot.account_id, lot.underlying_symbol, lot.open_order_id)
        if node not in first_open or lot.open_date < first_open[node]:
            first_open[node] = lot.open_date

    parent = {node: node for node in first_open}

    def find(node):
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:  # path compression
            parent[node], node = root, parent[node]
        return root

    for close in closes:
        if close.open_order_id is None or close.close_order_id is None:
            continue
        if close.close_order_id == close.open_order_id:
            continue
        new = (close.account_id, close.underlying_symbol, close.close_order_id)
        if new not in parent:
            continue  # the closing order opened nothing here: a plain close, not a roll
        old = (close.account_id, close.underlying_symbol, close.open_order_id)
        root_old, root_new = find(old), find(new)
        if root_old != root_new:
            parent[root_new] = root_old

    components: dict[tuple, list[tuple]] = defaultdict(list)
    for node in parent:
        components[find(node)].append(node)

    chain_of: dict[tuple, int] = {}
    n_chains = 0
    for members in components.values():
        if len(members) < 2:
            continue  # never rolled: not a chain
        root = min(members, key=lambda n: (first_open[n], n[2]))
        n_chains += 1
        for node in members:
            chain_of[node] = root[2]

    lot_chain: dict[int, int] = {}
    for lot in lots:
        cid = None
        if lot.open_order_id is not None:
            cid = chain_of.get((lot.account_id, lot.underlying_symbol, lot.open_order_id))
        lot.chain_id = cid
        if cid is not None:
            lot_chain[lot.lot_id] = cid
    for close in closes:
        close.chain_id = lot_chain.get(close.lot_id)
    return n_chains


# -- cash-flow helpers ---------------------------------------------------------
# Sign convention: positive = credit received, negative = debit paid.


def _open_cash(lot: Lot) -> Decimal:
    sign = Decimal(-1) if lot.side == Side.long else Decimal(1)
    return (sign * lot.open_price * lot.original_quantity * lot.multiplier).quantize(Q_MONEY)


def _close_cash(close: LotClose) -> Decimal:
    sign = Decimal(1) if close.side == Side.long else Decimal(-1)
    return (sign * close.close_price * close.quantity_closed * close.multiplier).quantize(Q_MONEY)


def _name_lots(lots: list[Lot]) -> str:
    """Structure name for the lots one order opened. Unlike the strategies
    view this needs no symbol parsing — lots store strike/expiry/right."""
    by_symbol: dict[str, LegShape] = {}
    for lot in lots:
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
    return name_structure(list(by_symbol.values()))


# -- chain analytics -------------------------------------------------------------


@dataclass
class ChainStep:
    """One order (or one order-less settlement event) within a chain."""

    order_id: int | None
    ts: datetime
    kind: str  # open | roll | close | expiration | assignment | exercise | cash_settlement
    opened: list[Lot] = field(default_factory=list)
    closes: list[LotClose] = field(default_factory=list)
    cash: Decimal = ZERO  # net credit(+)/debit(-) of the step, fees included
    fees: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    running_cash: Decimal = ZERO
    strategy_name: str = ""  # structure this step opened; "" if it only closed


@dataclass
class ChainSummary:
    chain_id: int
    account_id: str
    underlying_symbol: str
    first_open: datetime
    last_activity: datetime
    orders: int
    rolls: int
    closes: int
    realized_pnl: Decimal
    fees: Decimal
    open_quantity: Decimal  # remaining quantity across the chain's lots
    days_in_trade: int
    strategy_name: str = CUSTOM  # structure the latest opening order established

    @property
    def is_open(self) -> bool:
        return self.open_quantity > ZERO


@dataclass
class ChainDetail(ChainSummary):
    steps: list[ChainStep] = field(default_factory=list)
    open_lots: list[Lot] = field(default_factory=list)
    net_cash: Decimal = ZERO  # equals realized_pnl once the chain is fully closed


def _latest_opened(lots: list[Lot]) -> list[Lot]:
    """The lots opened by the chain's most recent opening order — what the
    campaign rolled into, i.e. what is held now while it is still open."""
    by_order: dict[object, list[Lot]] = defaultdict(list)
    for lot in lots:
        by_order[lot.open_order_id].append(lot)
    latest = max(by_order.values(), key=lambda g: max(l.open_date for l in g))
    return latest


def _summarize(chain_id: int, lots: list[Lot], closes: list[LotClose],
               detail: bool = False) -> ChainSummary | ChainDetail:
    open_orders = {l.open_order_id for l in lots if l.open_order_id is not None}
    close_orders = {c.close_order_id for c in closes if c.close_order_id is not None}
    first_open = min(l.open_date for l in lots)
    last_activity = max(
        [c.close_date for c in closes] + [l.open_date for l in lots]
    )
    open_qty = sum((l.remaining_quantity for l in lots), ZERO)
    end = date.today() if open_qty > ZERO else last_activity.date()
    kwargs = dict(
        chain_id=chain_id,
        account_id=lots[0].account_id,
        underlying_symbol=lots[0].underlying_symbol,
        first_open=first_open,
        last_activity=last_activity,
        orders=len(open_orders | close_orders),
        rolls=len(open_orders & close_orders),
        closes=len(closes),
        realized_pnl=sum((c.realized_pnl for c in closes), ZERO),
        fees=sum((l.open_fees for l in lots), ZERO) + sum((c.close_fees for c in closes), ZERO),
        open_quantity=open_qty,
        days_in_trade=(end - first_open.date()).days,
        strategy_name=_name_lots(_latest_opened(lots)),
    )
    if not detail:
        return ChainSummary(**kwargs)
    row = ChainDetail(**kwargs)
    row.steps = _build_steps(lots, closes)
    row.open_lots = sorted(
        (l for l in lots if l.remaining_quantity > ZERO), key=lambda l: l.open_date
    )
    row.net_cash = sum((s.cash for s in row.steps), ZERO)
    return row


def _build_steps(lots: list[Lot], closes: list[LotClose]) -> list[ChainStep]:
    steps: dict[object, ChainStep] = {}

    def step_for(key, order_id, ts) -> ChainStep:
        st = steps.get(key)
        if st is None:
            st = steps[key] = ChainStep(order_id=order_id, ts=ts, kind="")
        st.ts = min(st.ts, ts)
        return st

    for lot in lots:
        st = step_for(lot.open_order_id, lot.open_order_id, lot.open_date)
        st.opened.append(lot)
        st.cash += _open_cash(lot) - lot.open_fees
        st.fees += lot.open_fees
    for close in closes:
        key = close.close_order_id
        if key is None:  # sweep/settlement: group per reason+day
            key = ("settlement", close.close_reason.value, close.close_date.date())
        st = step_for(key, close.close_order_id, close.close_date)
        st.closes.append(close)
        st.cash += _close_cash(close) - close.close_fees
        st.fees += close.close_fees
        st.realized_pnl += close.realized_pnl

    ordered = sorted(steps.values(), key=lambda s: (s.ts, s.order_id or 0))
    running = ZERO
    for st in ordered:
        if st.opened and st.closes:
            st.kind = "roll"
        elif st.opened:
            st.kind = "open"
        elif st.order_id is None:
            st.kind = st.closes[0].close_reason.value
        else:
            st.kind = "close"
        if st.opened:
            st.strategy_name = _name_lots(st.opened)
        running += st.cash
        st.running_cash = running
        st.opened.sort(key=lambda l: l.symbol)
        st.closes.sort(key=lambda c: c.symbol)
    return ordered


def _load_grouped(session: Session, underlying: str | None = None,
                  account: str | None = None, chain_id: int | None = None):
    """Chain-stamped lots and closes grouped by (account, underlying, chain).
    The grouping key includes underlying because chain ids are assigned per
    (account, underlying) — a root order id can legitimately repeat across
    underlyings (a pairs order rolling both legs)."""
    lot_stmt = select(Lot).where(Lot.chain_id.is_not(None))
    close_stmt = select(LotClose).where(LotClose.chain_id.is_not(None))
    if underlying is not None:
        lot_stmt = lot_stmt.where(Lot.underlying_symbol == underlying)
        close_stmt = close_stmt.where(LotClose.underlying_symbol == underlying)
    if account is not None:
        lot_stmt = lot_stmt.where(Lot.account_id == account)
        close_stmt = close_stmt.where(LotClose.account_id == account)
    if chain_id is not None:
        lot_stmt = lot_stmt.where(Lot.chain_id == chain_id)
        close_stmt = close_stmt.where(LotClose.chain_id == chain_id)

    lots_by: dict[tuple, list[Lot]] = defaultdict(list)
    for lot in session.execute(lot_stmt).scalars():
        lots_by[(lot.account_id, lot.underlying_symbol, lot.chain_id)].append(lot)
    closes_by: dict[tuple, list[LotClose]] = defaultdict(list)
    for close in session.execute(close_stmt).scalars():
        closes_by[(close.account_id, close.underlying_symbol, close.chain_id)].append(close)
    return lots_by, closes_by


def chains(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    underlying: str | None = None,
    account: str | None = None,
    limit: int = 100,
) -> list[ChainSummary]:
    """Roll campaigns, newest activity first. The date range selects which
    chains appear (any close in range, or still open); the totals always span
    the whole campaign — a chain's PnL only makes sense end to end."""
    lots_by, closes_by = _load_grouped(session, underlying, account)

    def in_range(dt: datetime) -> bool:
        if start is not None and dt.date() < start:
            return False
        if end is not None and dt.date() > end:
            return False
        return True

    rows: list[ChainSummary] = []
    for key, lots in lots_by.items():
        closes = closes_by.get(key, [])
        row = _summarize(key[2], lots, closes)
        if not row.is_open and not any(in_range(c.close_date) for c in closes):
            continue
        rows.append(row)
    rows.sort(key=lambda r: r.last_activity, reverse=True)
    return rows[:limit]


def chain_detail(session: Session, chain_id: int) -> list[ChainDetail]:
    """Full step-by-step view of one chain id. Returns a list because the
    same root order id can head one chain per underlying (pairs orders);
    almost always a single element."""
    lots_by, closes_by = _load_grouped(session, chain_id=chain_id)
    details = [
        _summarize(chain_id, lots, closes_by.get(key, []), detail=True)
        for key, lots in lots_by.items()
    ]
    details.sort(key=lambda d: (d.account_id, d.underlying_symbol))
    return details
