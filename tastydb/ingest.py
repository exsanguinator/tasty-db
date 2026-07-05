"""Sync engine: pull transaction history into raw_transactions.

Idempotency: rows are keyed by TastyTrade's transaction id. Re-ingesting an
existing id is a no-op unless the payload changed (e.g. estimated fees
reconciled overnight), in which case the row is updated in place and flagged
for reprocessing.

Incremental mode re-fetches a small overlap window before the newest stored
transaction; dedupe-by-id makes the overlap free.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

log = logging.getLogger(__name__)

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .client import ApiError, TastyClient
from .models import Account, BalanceSnapshot, ProcessingStatus, RawTransaction, SyncRun

INCREMENTAL_OVERLAP_DAYS = 7

# a balance-snapshot history starting this much later than the account's first
# transaction is treated as truncated and backfilled from /net-liq/history
SNAPSHOT_GAP_TOLERANCE_DAYS = 14

# Fee fields on the Transaction model, each paired with a *-effect of
# Debit (a cost), Credit (a rebate), or None.
FEE_FIELDS = (
    "commission",
    "clearing-fees",
    "regulatory-fees",
    "proprietary-index-option-fees",
    "currency-conversion-fees",
    "other-charge",
)


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _utc_naive(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def total_fees(payload: dict) -> Decimal:
    """Signed sum of all fee fields; positive means a cost to the account."""
    total = Decimal("0")
    for field in FEE_FIELDS:
        amount = _dec(payload.get(field))
        if not amount:
            continue
        effect = payload.get(f"{field}-effect")
        if effect == "Credit":
            total -= amount
        else:  # Debit or unspecified
            total += amount
    return total


def parse_transaction(payload: dict) -> RawTransaction:
    return RawTransaction(
        id=int(payload["id"]),
        account_number=payload["account-number"],
        transaction_type=payload.get("transaction-type"),
        transaction_sub_type=payload.get("transaction-sub-type"),
        action=payload.get("action"),
        symbol=payload.get("symbol"),
        underlying_symbol=payload.get("underlying-symbol"),
        instrument_type=payload.get("instrument-type"),
        quantity=_dec(payload.get("quantity")),
        price=_dec(payload.get("price")),
        value=_dec(payload.get("value")),
        value_effect=payload.get("value-effect"),
        net_value=_dec(payload.get("net-value")),
        net_value_effect=payload.get("net-value-effect"),
        total_fees=total_fees(payload),
        executed_at=_utc_naive(payload["executed-at"]),
        transaction_date=(
            date.fromisoformat(payload["transaction-date"])
            if payload.get("transaction-date")
            else None
        ),
        payload=payload,
    )


def ingest_payloads(session: Session, payloads: Iterable[dict]) -> tuple[int, int, int]:
    """Store payloads into raw_transactions. Returns (fetched, inserted, updated)."""
    fetched = inserted = updated = 0
    for payload in payloads:
        fetched += 1
        parsed = parse_transaction(payload)
        existing = session.get(RawTransaction, parsed.id)
        if existing is None:
            session.add(parsed)
            inserted += 1
        elif existing.payload != payload:
            for attr in (
                "account_number", "transaction_type", "transaction_sub_type", "action",
                "symbol", "underlying_symbol", "instrument_type", "quantity", "price",
                "value", "value_effect", "net_value", "net_value_effect", "total_fees",
                "executed_at", "transaction_date", "payload",
            ):
                setattr(existing, attr, getattr(parsed, attr))
            existing.processing_status = ProcessingStatus.pending
            existing.processing_note = "payload updated on re-sync"
            existing.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            updated += 1
    return fetched, inserted, updated


def upsert_accounts(session: Session, accounts: list[dict]) -> None:
    """Cache account metadata (nickname etc.) so it's available offline."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for payload in accounts:
        number = payload["account-number"]
        row = session.get(Account, number) or Account(account_number=number)
        row.nickname = payload.get("nickname")
        row.account_type_name = payload.get("account-type-name")
        row.margin_or_cash = payload.get("margin-or-cash")
        row.payload = payload
        row.synced_at = now
        session.add(row)


def latest_transaction_date(session: Session, account_number: str) -> date | None:
    value = session.execute(
        select(func.max(RawTransaction.executed_at)).where(
            RawTransaction.account_number == account_number
        )
    ).scalar_one_or_none()
    return value.date() if value else None


def sync_account(
    session: Session,
    client: TastyClient,
    account_number: str,
    backfill: bool = False,
    since: date | None = None,
) -> SyncRun:
    """Sync one account. Backfill pulls full history; incremental starts a bit
    before the newest stored transaction (falling back to backfill when the
    account has no data yet)."""
    if since is not None:
        start_date = since
    elif backfill:
        start_date = None  # full history
    else:
        newest = latest_transaction_date(session, account_number)
        start_date = (
            newest - timedelta(days=INCREMENTAL_OVERLAP_DAYS) if newest else None
        )

    run = SyncRun(
        account_number=account_number,
        mode="backfill" if (backfill or start_date is None) and since is None else "incremental",
        start_date_used=start_date,
        started_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(run)

    payloads = client.iter_transactions(account_number, start_date=start_date)
    run.fetched, run.inserted, run.updated = ingest_payloads(session, payloads)
    run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.commit()
    return run


# -- balance snapshots -------------------------------------------------------


def earliest_transaction_date(session: Session, account_number: str) -> date | None:
    value = session.execute(
        select(func.min(RawTransaction.executed_at)).where(
            RawTransaction.account_number == account_number
        )
    ).scalar_one_or_none()
    return value.date() if value else None


def _upsert_snapshot(
    session: Session,
    *,
    account_number: str,
    snapshot_date: date,
    time_of_day: str,
    nlv: Decimal,
    cash_balance: Decimal | None,
    source: str,
    payload: dict | None,
) -> int:
    row = session.get(BalanceSnapshot, (account_number, snapshot_date, time_of_day))
    if row is None:
        session.add(BalanceSnapshot(
            account_number=account_number,
            snapshot_date=snapshot_date,
            time_of_day=time_of_day,
            net_liquidating_value=nlv,
            cash_balance=cash_balance,
            source=source,
            payload=payload,
        ))
        return 1
    if row.source == "snapshot" and source == "netliq_history":
        return 0  # never replace a real snapshot with derived history
    if (row.net_liquidating_value, row.cash_balance, row.source) == (nlv, cash_balance, source):
        return 0
    row.net_liquidating_value = nlv
    row.cash_balance = cash_balance
    row.source = source
    row.payload = payload
    row.fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return 1


def _netliq_time_to_date(raw) -> date | None:
    """The net-liq/history `time` field is unverified in the docs; accept epoch
    seconds/millis or an ISO datetime (possibly with a trailing [UTC] zone id)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e11:  # epoch millis
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    text = str(raw).split("[", 1)[0]
    try:
        return _utc_naive(text).date()
    except ValueError:
        return None


def sync_balance_snapshots(
    session: Session,
    client: TastyClient,
    account_number: str,
    backfill: bool = False,
) -> tuple[int, date | None, date | None]:
    """Fetch EOD balance snapshots for one account into balance_snapshots.
    Idempotent (upsert by PK). Returns (upserted, min_date, max_date) over the
    account's stored rows. If the endpoint's history starts well after the
    account's first transaction, older dates are backfilled from
    /net-liq/history daily closes (source='netliq_history')."""
    latest = session.execute(
        select(func.max(BalanceSnapshot.snapshot_date)).where(
            BalanceSnapshot.account_number == account_number
        )
    ).scalar_one_or_none()
    first_txn = earliest_transaction_date(session, account_number)
    if backfill or latest is None:
        start = first_txn
    else:
        start = latest - timedelta(days=INCREMENTAL_OVERLAP_DAYS)

    upserted = 0
    for item in client.iter_balance_snapshots(account_number, start_date=start):
        raw_date = item.get("snapshot-date")
        nlv = _dec(item.get("net-liquidating-value"))
        if raw_date is None or nlv is None:
            continue  # e.g. the current-balance item the endpoint appends
        upserted += _upsert_snapshot(
            session,
            account_number=account_number,
            snapshot_date=date.fromisoformat(raw_date),
            time_of_day=item.get("time-of-day") or "EOD",
            nlv=nlv,
            cash_balance=_dec(item.get("cash-balance")),
            source="snapshot",
            payload=item,
        )
    session.flush()

    bounds = session.execute(
        select(
            func.min(BalanceSnapshot.snapshot_date),
            func.max(BalanceSnapshot.snapshot_date),
        ).where(BalanceSnapshot.account_number == account_number)
    ).one()
    earliest_snap: date | None = bounds[0]

    gap = (
        first_txn is not None
        and (earliest_snap is None
             or (earliest_snap - first_txn).days > SNAPSHOT_GAP_TOLERANCE_DAYS)
    )
    if gap:
        log.info(
            "%s: balance snapshots start %s but first transaction is %s — "
            "backfilling from net-liq history",
            account_number, earliest_snap, first_txn,
        )
        try:
            items = client.net_liq_history(account_number, time_back="all")
        except ApiError as exc:
            log.warning("%s: net-liq history unavailable: %s", account_number, exc)
            items = []
        for item in items:
            day = _netliq_time_to_date(item.get("time"))
            close = _dec(item.get("close"))
            if day is None or close is None:
                continue
            if earliest_snap is not None and day >= earliest_snap:
                continue  # real snapshots win from that date on
            upserted += _upsert_snapshot(
                session,
                account_number=account_number,
                snapshot_date=day,
                time_of_day="EOD",
                nlv=close,
                cash_balance=None,
                source="netliq_history",
                payload=item,
            )
        session.flush()
        bounds = session.execute(
            select(
                func.min(BalanceSnapshot.snapshot_date),
                func.max(BalanceSnapshot.snapshot_date),
            ).where(BalanceSnapshot.account_number == account_number)
        ).one()

    session.commit()
    return upserted, bounds[0], bounds[1]
