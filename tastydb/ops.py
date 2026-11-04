"""Database maintenance operations shared by the CLI and the web dashboard.

Each function does the work of one CLI command (`accounts`, `sync`, `process`,
`status`) and returns data; `cli.py` prints it and `web/app.py` renders it,
so both front ends run exactly the same code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .cashflows import CashFlow, unclassified_flows
from .client import ApiError, TastyClient
from .ingest import sync_account, sync_balance_snapshots, upsert_accounts
from .instruments import MetaProvider
from .matching import rebuild_lots
from .models import (
    Account,
    BalanceSnapshot,
    Lot,
    LotClose,
    ProcessingStatus,
    RawTransaction,
    SyncRun,
)

ATTENTION_LIMIT = 20


# ---------------------------------------------------------------- accounts


@dataclass(frozen=True)
class AccountRow:
    account_number: str
    nickname: str


def list_accounts(
    session: Session, client: TastyClient | None
) -> tuple[list[AccountRow], bool]:
    """Accounts from the API (refreshing the cache) or, with no client, the
    cached copy. Returns (rows, offline)."""
    if client is not None:
        payloads = client.accounts()
        upsert_accounts(session, payloads)
        session.commit()
        rows = [AccountRow(a["account-number"], a.get("nickname") or "") for a in payloads]
        return rows, False
    cached = session.execute(select(Account).order_by(Account.account_number)).scalars()
    return [AccountRow(a.account_number, a.nickname or "") for a in cached], True


# ---------------------------------------------------------------- sync


@dataclass
class SyncResult:
    account_number: str
    mode: str
    start_date_used: date | None
    fetched: int
    inserted: int
    updated: int
    snapshots_upserted: int = 0
    snapshots_span: tuple[date, date] | None = None
    snapshot_error: str | None = None

    @property
    def transactions_line(self) -> str:
        return (
            f"{self.account_number}: {self.mode} from {self.start_date_used or 'beginning'} — "
            f"fetched {self.fetched}, inserted {self.inserted}, updated {self.updated}"
        )

    @property
    def snapshots_line(self) -> str:
        if self.snapshot_error is not None:
            return f"{self.account_number}: balance snapshots failed: {self.snapshot_error}"
        span = (
            f"{self.snapshots_span[0]} → {self.snapshots_span[1]}"
            if self.snapshots_span else "none stored"
        )
        return f"{self.account_number}: {self.snapshots_upserted} balance snapshots upserted ({span})"


def run_sync(
    session: Session,
    client: TastyClient,
    account: str | None = None,
    backfill: bool = False,
    since: date | None = None,
    progress: Callable[[str], None] | None = None,
    on_result: Callable[[SyncResult], None] | None = None,
) -> list[SyncResult]:
    """Pull transactions + balance snapshots for one or all accounts.
    `progress` (if given) receives a stage message before each account and
    `on_result` each account's result as soon as it is done."""
    account_rows = client.accounts()
    numbers = [account] if account else [a["account-number"] for a in account_rows]
    upsert_accounts(session, account_rows)
    results: list[SyncResult] = []
    for number in numbers:
        if progress:
            progress(f"syncing {number}")
        run = sync_account(session, client, number, backfill=backfill, since=since)
        result = SyncResult(
            account_number=number,
            mode=run.mode,
            start_date_used=run.start_date_used,
            fetched=run.fetched,
            inserted=run.inserted,
            updated=run.updated,
        )
        try:
            upserted, lo, hi = sync_balance_snapshots(session, client, number, backfill=backfill)
            result.snapshots_upserted = upserted
            result.snapshots_span = (lo, hi) if lo else None
        except ApiError as exc:
            result.snapshot_error = str(exc)
        results.append(result)
        if on_result:
            on_result(result)
    session.commit()
    return results


# ---------------------------------------------------------------- process


def run_process(
    session: Session,
    client: TastyClient | None,
    method: str = "fifo",
    grace_days: int = 4,
) -> dict:
    """Classify raw transactions and rebuild lots/closes; returns rebuild stats."""
    return rebuild_lots(session, MetaProvider(session, client), method=method,
                        grace_days=grace_days)


def process_summary(stats: dict) -> str:
    return (
        f"{stats['transactions']} transactions -> {stats['events']} events, "
        f"{stats['closes']} closes, {stats['open_lots']} open lots "
        f"({stats['expired_worthless']} swept as worthless expiration), "
        f"{stats['chains']} roll chains"
    )


# ---------------------------------------------------------------- status


@dataclass
class AccountActivity:
    """Per-account data coverage, for the dashboard's accounts table."""

    account_number: str
    transactions: int = 0
    first_txn: datetime | None = None
    last_txn: datetime | None = None
    last_sync: SyncRun | None = None
    last_snapshot: date | None = None


@dataclass
class DbStatus:
    status_counts: list[tuple[ProcessingStatus, int]]
    open_lots: int
    closes: int
    problems: list[RawTransaction]
    problem_count: int
    unclassified: list[CashFlow]
    activity: dict[str, AccountActivity] = field(default_factory=dict)

    @property
    def raw_total(self) -> int:
        return sum(count for _, count in self.status_counts)

    @property
    def last_sync_at(self) -> datetime | None:
        times = [a.last_sync.finished_at or a.last_sync.started_at
                 for a in self.activity.values() if a.last_sync]
        return max(times, default=None)


def db_status(session: Session, with_activity: bool = False) -> DbStatus:
    """Everything `tastydb status` reports; `with_activity` adds per-account
    coverage (transaction span, last sync run, last balance snapshot)."""
    counts = session.execute(
        select(RawTransaction.processing_status, func.count())
        .group_by(RawTransaction.processing_status)
    ).all()
    open_lots = session.execute(
        select(func.count()).select_from(Lot).where(Lot.remaining_quantity > 0)
    ).scalar_one()
    closes = session.execute(select(func.count()).select_from(LotClose)).scalar_one()
    bad = RawTransaction.processing_status.in_(
        [ProcessingStatus.unsupported, ProcessingStatus.error]
    )
    problems = session.execute(
        select(RawTransaction).where(bad).order_by(RawTransaction.executed_at)
        .limit(ATTENTION_LIMIT)
    ).scalars().all()
    problem_count = session.execute(
        select(func.count()).select_from(RawTransaction).where(bad)
    ).scalar_one()
    status = DbStatus(
        status_counts=[(s, n) for s, n in counts],
        open_lots=open_lots,
        closes=closes,
        problems=list(problems),
        problem_count=problem_count,
        unclassified=unclassified_flows(session),
    )
    if with_activity:
        status.activity = _account_activity(session)
    return status


def _account_activity(session: Session) -> dict[str, AccountActivity]:
    out: dict[str, AccountActivity] = {}

    def get(number: str) -> AccountActivity:
        return out.setdefault(number, AccountActivity(number))

    for number, n, lo, hi in session.execute(
        select(RawTransaction.account_number, func.count(),
               func.min(RawTransaction.executed_at), func.max(RawTransaction.executed_at))
        .group_by(RawTransaction.account_number)
    ):
        a = get(number)
        a.transactions, a.first_txn, a.last_txn = n, lo, hi

    latest_ids = (
        select(func.max(SyncRun.id)).group_by(SyncRun.account_number).scalar_subquery()
    )
    for run in session.execute(select(SyncRun).where(SyncRun.id.in_(latest_ids))).scalars():
        get(run.account_number).last_sync = run

    for number, last in session.execute(
        select(BalanceSnapshot.account_number, func.max(BalanceSnapshot.snapshot_date))
        .group_by(BalanceSnapshot.account_number)
    ):
        get(number).last_snapshot = last
    return out
