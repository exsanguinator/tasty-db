"""Account-level returns: NLV series assembly, TWR chaining with flow
attachment, XIRR, snapshot sync idempotency, and the /performance page."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from tastydb.ingest import ingest_payloads, sync_balance_snapshots
from tastydb.models import BalanceSnapshot
from tastydb.returns import nlv_series, period_returns, twr_index

from .test_cashflows import make_mm

ACCT = "5WT00001"


def snap(session, day, nlv, account=ACCT, source="snapshot"):
    session.add(BalanceSnapshot(
        account_number=account, snapshot_date=date.fromisoformat(day),
        time_of_day="EOD", net_liquidating_value=Decimal(str(nlv)), source=source,
    ))


def deposit(session, day, amount, account=ACCT):
    ingest_payloads(session, [make_mm(
        sub_type="Deposit", description="ACH DEPOSIT", value=amount,
        effect="Credit", account=account, executed_at=f"{day}T12:00:00+00:00",
    )])


def test_flat_nlv_with_mid_period_deposit_is_zero_return(session):
    snap(session, "2026-01-01", 1000)
    snap(session, "2026-01-10", 2000)
    deposit(session, "2026-01-05", 1000.0)
    session.commit()

    pr = period_returns(session, ACCT)
    assert pr.net_flows == Decimal("1000")
    assert pr.pnl == Decimal("0")
    assert pr.twr == Decimal("0")
    assert pr.twr_annualized is None  # 9-day window, below the threshold


def test_doubling_with_no_flows(session):
    snap(session, "2025-01-01", 1000)
    snap(session, "2026-01-01", 2000)
    session.commit()

    pr = period_returns(session, ACCT)
    assert pr.twr == Decimal("1")
    assert pr.pnl == Decimal("1000")
    assert pr.days == 365
    # (1+100%)^(365.25/365) - 1, and XIRR agrees when there are no mid flows
    assert float(pr.twr_annualized) == pytest.approx(1.00095, abs=1e-3)
    assert float(pr.xirr) == pytest.approx(float(pr.twr_annualized), abs=1e-4)


def test_deposit_before_drawdown_twr_vs_xirr(session):
    # +100% on little money, then -50% right after a big deposit:
    # time-weighted is flat, but most dollars only saw the loss.
    snap(session, "2026-01-01", 1000)
    snap(session, "2026-02-01", 2000)
    snap(session, "2026-02-10", 12000)  # EOD after the deposit landed
    snap(session, "2026-03-01", 6000)
    deposit(session, "2026-02-10", 10000.0)
    session.commit()

    pr = period_returns(session, ACCT)
    assert pr.twr == Decimal("0")  # 2.0 × 0.5 − 1
    assert pr.pnl == Decimal("-5000")
    assert pr.xirr is not None and pr.xirr < 0


def test_zero_start_restart_skips_undefined_link(session):
    snap(session, "2026-01-01", 0)
    snap(session, "2026-01-02", 1000)
    snap(session, "2026-01-10", 1100)
    deposit(session, "2026-01-02", 1000.0)
    session.commit()

    pr = period_returns(session, ACCT)
    assert pr.twr == Decimal("0.1")
    assert pr.pnl == Decimal("100")


def test_weekend_flow_attaches_to_next_snapshot(session):
    snap(session, "2026-01-02", 1000)  # Friday
    snap(session, "2026-01-05", 1500)  # Monday
    deposit(session, "2026-01-03", 500.0)  # Saturday
    session.commit()

    pr = period_returns(session, ACCT)
    assert pr.twr == Decimal("0")
    assert pr.pnl == Decimal("0")


def test_combined_series_requires_full_coverage(session):
    snap(session, "2026-01-01", 100, account="A1")
    snap(session, "2026-01-02", 110, account="A1")
    snap(session, "2026-01-03", 120, account="A1")
    snap(session, "2026-01-01", 50, account="A2")
    snap(session, "2026-01-03", 60, account="A2")  # missing Jan 2 inside its span
    snap(session, "2026-01-03", 30, account="A3")  # opened later: span starts Jan 3
    session.commit()

    series = nlv_series(session)
    assert series == [
        (date(2026, 1, 1), Decimal("150")),  # A3 not yet covering — excluded
        (date(2026, 1, 3), Decimal("210")),  # Jan 2 dropped: A2 has no row
    ]
    assert nlv_series(session, account="A1", start=date(2026, 1, 2)) == [
        (date(2026, 1, 2), Decimal("110")),
        (date(2026, 1, 3), Decimal("120")),
    ]


def test_twr_index_growth_of_100(session):
    snap(session, "2026-01-01", 1000)
    snap(session, "2026-02-01", 2000)
    snap(session, "2026-02-10", 12000)
    snap(session, "2026-03-01", 6000)
    deposit(session, "2026-02-10", 10000.0)
    session.commit()

    assert twr_index(session, ACCT) == [
        (date(2026, 1, 1), Decimal("100")),
        (date(2026, 2, 1), Decimal("200")),
        (date(2026, 2, 10), Decimal("200")),  # deposit is flow, not growth
        (date(2026, 3, 1), Decimal("100")),
    ]


def test_single_snapshot_yields_no_return(session):
    snap(session, "2026-01-01", 1000)
    session.commit()
    pr = period_returns(session, ACCT)
    assert pr.days == 0 and pr.twr is None and pr.pnl is None
    assert pr.end_nlv == Decimal("1000")


class FakeClient:
    def __init__(self, snapshots, history=()):
        self.snapshots = snapshots
        self.history = list(history)
        self.history_calls = 0

    def iter_balance_snapshots(self, account_number, start_date=None, **_):
        for item in self.snapshots:
            if start_date and date.fromisoformat(item["snapshot-date"]) < start_date:
                continue
            yield item

    def net_liq_history(self, account_number, time_back="all"):
        self.history_calls += 1
        return self.history


def _snap_item(day, nlv, cash="0"):
    return {"snapshot-date": day, "time-of-day": "EOD",
            "net-liquidating-value": nlv, "cash-balance": cash}


def test_sync_balance_snapshots_idempotent(session):
    client = FakeClient([_snap_item("2026-01-02", "1000.0"), _snap_item("2026-01-03", "1010.0")])
    upserted, lo, hi = sync_balance_snapshots(session, client, ACCT)
    assert (upserted, lo, hi) == (2, date(2026, 1, 2), date(2026, 1, 3))
    assert sync_balance_snapshots(session, client, ACCT)[0] == 0  # no dupes

    client.snapshots[1] = _snap_item("2026-01-03", "1020.0")  # overnight reconciliation
    assert sync_balance_snapshots(session, client, ACCT)[0] == 1
    row = session.get(BalanceSnapshot, (ACCT, date(2026, 1, 3), "EOD"))
    assert row.net_liquidating_value == Decimal("1020.0")


def test_netliq_backfill_fills_gap_without_overwriting(session):
    # a transaction long before the earliest snapshot ⇒ history is truncated
    deposit(session, "2025-01-02", 100.0)
    session.commit()
    client = FakeClient(
        [_snap_item("2026-01-02", "1000.0")],
        history=[
            {"time": "2025-01-02T00:00:00+00:00[UTC]", "close": "800.0"},
            {"time": 1736121600000, "close": "900.0"},  # 2025-01-06 epoch millis
            {"time": "2026-01-02T00:00:00+00:00", "close": "999.0"},  # snapshot wins
        ],
    )
    upserted, lo, hi = sync_balance_snapshots(session, client, ACCT)
    assert client.history_calls == 1
    assert (upserted, lo, hi) == (3, date(2025, 1, 2), date(2026, 1, 2))

    rows = {r.snapshot_date: r for r in session.execute(select(BalanceSnapshot)).scalars()}
    assert rows[date(2025, 1, 2)].source == "netliq_history"
    assert rows[date(2025, 1, 2)].net_liquidating_value == Decimal("800.0")
    assert rows[date(2025, 1, 6)].net_liquidating_value == Decimal("900.0")
    assert rows[date(2026, 1, 2)].source == "snapshot"
    assert rows[date(2026, 1, 2)].net_liquidating_value == Decimal("1000.0")


def test_dashboard_performance_page(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as OrmSession

    from .test_phase4 import _dashboard_client

    client = _dashboard_client([make_mm(
        sub_type="Deposit", description="ACH DEPOSIT", value=1000.0,
        effect="Credit", executed_at="2026-01-05T12:00:00+00:00",
    )], tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path}/dash.sqlite3")
    with OrmSession(engine) as s:
        snap(s, "2026-01-01", 1000)
        snap(s, "2026-01-10", 2500)
        s.commit()

    page = client.get("/performance")
    assert page.status_code == 200
    assert "TWR" in page.text and "XIRR" in page.text
    assert "ACH DEPOSIT" in page.text
    assert "+$500.00" in page.text  # PnL = 2500 − 1000 − 1000

    empty = client.get("/performance?start=2030-01-01")
    assert empty.status_code == 200
    assert "Not enough balance snapshots" in empty.text
