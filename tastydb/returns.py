"""Account-level returns from NLV snapshots + external cash flows.

Two standard measures over any window covered by balance_snapshots:

- TWR (time-weighted): daily-chained with the end-of-day flow convention —
  for consecutive snapshot dates d_prev < d with values V_prev, V and external
  flows F dated in (d_prev, d], the link is r = (V - F) / V_prev - 1, and the
  window return chains (1+r) across links. Flow timing is neutralized, which
  makes TWR the number to compare against a benchmark.
- XIRR (money-weighted): the annualized rate x solving NPV = 0 over the
  investor's dated cash flows: -V_start at the window open, -deposit /
  +withdrawal for each external flow, +V_end at the close. Answers "what did
  my dollars earn" including flow timing.

Dollar PnL over the window is the model-free identity
V_end - V_start - net external flows.

Conventions: the window base is the first snapshot >= start (its NLV already
contains that day's flows — EOD convention), the window close is the last
snapshot <= end. Links where the base NLV is not positive are skipped (a
deposit-funded restart from zero has no defined return). Rates are ratios,
not money — reported to 6 decimal places, never forced to money quantization.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .cashflows import CashFlow, external_flows
from .models import BalanceSnapshot

Q_MONEY = Decimal("0.0001")
MIN_ANNUALIZE_DAYS = 30


@dataclass(frozen=True)
class PeriodReturns:
    account: str | None  # None = all accounts combined
    start_date: date | None
    end_date: date | None
    start_nlv: Decimal | None
    end_nlv: Decimal | None
    net_flows: Decimal  # external only, signed (+ = into the account)
    pnl: Decimal | None  # end - start - net_flows
    twr: Decimal | None  # total for the window, 0.1234 = +12.34%
    twr_annualized: Decimal | None  # only when the window spans >= 30 days
    xirr: Decimal | None  # annualized by definition
    days: int


def nlv_series(
    session: Session,
    account: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[tuple[date, Decimal]]:
    """EOD net-liq series, ascending. With account=None, accounts are summed
    per date; a date is included only when every account whose own snapshot
    span covers it has a row — a missing account would read as a phantom
    withdrawal-sized dip."""
    stmt = (
        select(BalanceSnapshot)
        .where(BalanceSnapshot.time_of_day == "EOD")
        .order_by(BalanceSnapshot.snapshot_date)
    )
    if account:
        stmt = stmt.where(BalanceSnapshot.account_number == account)
    rows = session.execute(stmt).scalars().all()

    by_account: dict[str, dict[date, Decimal]] = {}
    for row in rows:
        by_account.setdefault(row.account_number, {})[row.snapshot_date] = (
            row.net_liquidating_value
        )

    spans = {
        acct: (min(dates), max(dates)) for acct, dates in by_account.items() if dates
    }
    series: list[tuple[date, Decimal]] = []
    for day in sorted({d for dates in by_account.values() for d in dates}):
        covering = [a for a, (lo, hi) in spans.items() if lo <= day <= hi]
        if any(day not in by_account[a] for a in covering):
            continue
        series.append((day, sum((by_account[a][day] for a in covering), Decimal("0"))))

    if start is not None:
        series = [p for p in series if p[0] >= start]
    if end is not None:
        series = [p for p in series if p[0] <= end]
    return series


def _links(
    series: list[tuple[date, Decimal]], flows: list[CashFlow]
) -> list[tuple[date, Decimal]]:
    """Per-snapshot growth factors [(date, 1+r)], flows attached to the link
    ending at their next snapshot date.

    The EOD convention is exact to within one day when snapshots are daily.
    Across a multi-day snapshot gap a flow larger than the base can push the
    factor negative (nonsense — it would flip the sign of the whole chain);
    such links are data-gap anomalies and are skipped, like zero-base links."""
    flow_amounts = sorted(((f.date, f.amount) for f in flows))
    links: list[tuple[date, Decimal]] = []
    idx = 0
    for (d_prev, v_prev), (d, v) in zip(series, series[1:]):
        flow = Decimal("0")
        while idx < len(flow_amounts) and flow_amounts[idx][0] <= d:
            if flow_amounts[idx][0] > d_prev:
                flow += flow_amounts[idx][1]
            idx += 1
        factor = (v - flow) / v_prev if v_prev > 0 else Decimal("0")
        if factor > 0:
            links.append((d, factor))
        else:
            links.append((d, Decimal("1")))  # zero base or gap anomaly: skip
    return links


def _xirr(flows: list[tuple[date, Decimal]]) -> float | None:
    """Bisection on NPV; None when no root is bracketed (e.g. all flows the
    same sign). The wide upper bound matters: short windows with large gains
    annualize to huge rates."""
    if len(flows) < 2:
        return None
    # a rate is only defined when money went both ways (guards the all-zero
    # case too, where NPV≡0 and any rate would "solve" it)
    if not (any(a > 0 for _, a in flows) and any(a < 0 for _, a in flows)):
        return None
    t0 = flows[0][0]
    cfs = [((d - t0).days / 365.25, float(a)) for d, a in flows]

    def npv(rate: float) -> float:
        return sum(a / (1.0 + rate) ** t for t, a in cfs)

    # wide bracket: short windows annualize to extreme rates in both directions
    lo, hi = -0.999999, 1e6
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9 or hi - lo < 1e-10:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def _rate(value: float | Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(str(round(float(value), 6)))


def period_returns(
    session: Session,
    account: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> PeriodReturns:
    series = nlv_series(session, account, start, end)
    if len(series) < 2:
        only = series[0] if series else (None, None)
        return PeriodReturns(
            account=account, start_date=only[0], end_date=only[0],
            start_nlv=only[1], end_nlv=only[1], net_flows=Decimal("0"),
            pnl=None, twr=None, twr_annualized=None, xirr=None, days=0,
        )

    (d0, v0), (dn, vn) = series[0], series[-1]
    flows = [f for f in external_flows(session, account) if d0 < f.date <= dn]
    net = sum((f.amount for f in flows), Decimal("0"))
    pnl = (vn - v0 - net).quantize(Q_MONEY)

    growth = Decimal("1")
    for _, factor in _links(series, flows):
        growth *= factor
    twr = growth - 1

    days = (dn - d0).days
    annualized = None
    if days >= MIN_ANNUALIZE_DAYS and twr > -1:
        annualized = (1.0 + float(twr)) ** (365.25 / days) - 1.0

    xirr_flows = (
        [(d0, -v0)]
        + [(f.date, -f.amount) for f in flows]
        + [(dn, vn)]
    )
    return PeriodReturns(
        account=account, start_date=d0, end_date=dn,
        start_nlv=v0, end_nlv=vn, net_flows=net.quantize(Q_MONEY), pnl=pnl,
        twr=_rate(twr), twr_annualized=_rate(annualized),
        xirr=_rate(_xirr(xirr_flows)), days=days,
    )


def twr_index(
    session: Session,
    account: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[tuple[date, Decimal]]:
    """Growth-of-$100 series (flow-neutral) — the benchmark-comparable line."""
    series = nlv_series(session, account, start, end)
    if not series:
        return []
    d0, dn = series[0][0], series[-1][0]
    flows = [f for f in external_flows(session, account) if d0 < f.date <= dn]
    index = [(d0, Decimal("100"))]
    growth = Decimal("1")
    for day, factor in _links(series, flows):
        growth *= factor
        index.append((day, (Decimal("100") * growth).quantize(Q_MONEY)))
    return index
