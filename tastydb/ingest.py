"""Sync engine: pull transaction history into raw_transactions.

Idempotency: rows are keyed by TastyTrade's transaction id. Re-ingesting an
existing id is a no-op unless the payload changed (e.g. estimated fees
reconciled overnight), in which case the row is updated in place and flagged
for reprocessing.

Incremental mode re-fetches a small overlap window before the newest stored
transaction; dedupe-by-id makes the overlap free.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .client import TastyClient
from .models import ProcessingStatus, RawTransaction, SyncRun

INCREMENTAL_OVERLAP_DAYS = 7

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
