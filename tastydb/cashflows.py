"""Classify Money Movement transactions into cash flows.

External flows — money crossing the account boundary (ACH deposits and
disbursements, wires, ACAT transfers, journals between the user's own
accounts, tax withholding) — are what return math must neutralize. Everything
else (dividends, interest, fees, futures mark-to-market, lending income) is
performance and stays internal.

The transaction-sub-type vocabulary is undocumented and unreliable: real data
books credit interest, dividends, and commission rebates under sub-type
"Deposit", and margin interest under "Withdrawal". Classification therefore
checks, in order: an instrument symbol (dividends/mark-to-market are tied to
one; true flows never are), description patterns, then sub-type. Rows whose
sub-type claims a flow (Deposit/Withdrawal/Transfer) but whose description
matches no known pattern are flagged `unclassified_flow` — still treated as
external, since they almost certainly are — and surfaced by `tastydb status`,
mirroring the `unsupported` philosophy for position rows.

Flows are computed on demand from raw_transactions (about a thousand rows for
years of history); there is no derived table and nothing here runs during
`process`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .classify import _signed_value
from .models import RawTransaction


@dataclass(frozen=True)
class CashFlow:
    txn_id: int
    account_number: str
    date: date
    amount: Decimal  # signed: positive = money into the account
    category: str
    external: bool  # True = deposit/withdrawal-like, excluded from performance
    sub_type: str | None
    description: str


# (pattern, category, external) — first match wins. Internal traps come first
# so an "INTEREST ON CREDIT BALANCE" booked under sub-type Deposit is never
# mistaken for a contribution.
_DESCRIPTION_RULES: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (re.compile(r"INTEREST ON CREDIT BALANCE", re.I), "interest", False),
    # margin interest, e.g. "FROM 05/16 THRU 06/15 @ 6 1/2%"
    (re.compile(r"FROM \d{2}/\d{2} THRU .*@", re.I), "margin_interest", False),
    (re.compile(r"COMMISSION (REBATE|ADJUSTMENT)", re.I), "fee", False),
    (re.compile(r"ACH (DEPOSIT|RECEIPT)", re.I), "deposit", True),
    (re.compile(r"ACH (DISBURSEMENT|PAYMENT)", re.I), "withdrawal", True),
    (re.compile(r"\bWIRE\b", re.I), "wire", True),
    (re.compile(r"TRANSFER (FROM|TO)", re.I), "transfer", True),
    (re.compile(r"JOURNAL (FROM|TO) ACCOUNT", re.I), "journal", True),
    (re.compile(r"(FED|STATE) WITHHOLDING", re.I), "withholding", True),
)

_INTERNAL_SUBTYPES = {
    "dividend": "dividend",
    "credit interest": "interest",
    "debit interest": "margin_interest",
    "fee": "fee",
    "balance adjustment": "fee",
    "mark to market": "futures_mtm",
    "fully paid stock lending income": "lending_income",
}
_FLOW_SUBTYPES = frozenset({"deposit", "withdrawal", "transfer"})


def classify_flow(txn: RawTransaction) -> CashFlow:
    """Classify one Money Movement transaction (pure function of the raw row)."""
    description = str((txn.payload or {}).get("description") or "")
    sub_type = (txn.transaction_sub_type or "").strip().lower()

    if txn.symbol:
        # cash tied to an instrument (dividends, futures mark-to-market) is
        # performance, whatever the sub-type claims
        category, external = _INTERNAL_SUBTYPES.get(sub_type, "dividend"), False
    else:
        for pattern, cat, ext in _DESCRIPTION_RULES:
            if pattern.search(description):
                category, external = cat, ext
                break
        else:
            if sub_type in _INTERNAL_SUBTYPES:
                category, external = _INTERNAL_SUBTYPES[sub_type], False
            elif sub_type in _FLOW_SUBTYPES:
                category, external = "unclassified_flow", True
            else:
                category, external = "other", False

    return CashFlow(
        txn_id=txn.id,
        account_number=txn.account_number,
        date=txn.transaction_date or txn.executed_at.date(),
        amount=_signed_value(txn),
        category=category,
        external=external,
        sub_type=txn.transaction_sub_type,
        description=description,
    )


def classify_flows(
    session: Session,
    account: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[CashFlow]:
    """All Money Movement rows as classified CashFlows, ordered by date.
    The date filter uses the flow date (transaction-date), inclusive."""
    stmt = (
        select(RawTransaction)
        .where(RawTransaction.transaction_type == "Money Movement")
        .order_by(RawTransaction.executed_at, RawTransaction.id)
    )
    if account:
        stmt = stmt.where(RawTransaction.account_number == account)
    flows = [classify_flow(t) for t in session.execute(stmt).scalars()]
    if start is not None:
        flows = [f for f in flows if f.date >= start]
    if end is not None:
        flows = [f for f in flows if f.date <= end]
    return flows


def external_flows(
    session: Session,
    account: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[CashFlow]:
    return [f for f in classify_flows(session, account, start, end) if f.external]


def unclassified_flows(session: Session) -> list[CashFlow]:
    """Flow-claiming rows no rule recognized — shown by `tastydb status`."""
    return [f for f in classify_flows(session) if f.category == "unclassified_flow"]
