"""Money Movement → cash-flow classification: external flows (deposits,
withdrawals, journals, withholding) vs performance items that masquerade as
flows (interest and dividends booked under sub-type Deposit, margin interest
booked as Withdrawal)."""

from datetime import date
from decimal import Decimal

from tastydb.cashflows import classify_flows, external_flows, unclassified_flows
from tastydb.ingest import ingest_payloads

from .conftest import make_txn


def make_mm(*, sub_type, description, value, effect, symbol=None, account=None,
            executed_at="2026-01-15T12:00:00+00:00", txn_id=None):
    payload = make_txn(
        txn_type="Money Movement", sub_type=sub_type, symbol=symbol,
        instrument_type="Equity" if symbol else None, quantity=0,
        value=value, value_effect=effect, executed_at=executed_at,
        txn_id=txn_id, description=description,
    )
    if account:
        payload["account-number"] = account
    return payload


def _flows(session, *payloads):
    ingest_payloads(session, payloads)
    session.commit()
    return classify_flows(session)


def test_ach_deposit_is_external(session):
    (f,) = _flows(session, make_mm(sub_type="Deposit", description="ACH DEPOSIT",
                                   value=250.0, effect="Credit"))
    assert (f.category, f.external, f.amount) == ("deposit", True, Decimal("250"))
    assert f.date == date(2026, 1, 15)


def test_ach_disbursement_is_external_withdrawal(session):
    (f,) = _flows(session, make_mm(sub_type="Withdrawal", description="ACH DISBURSEMENT",
                                   value=103.0, effect="Debit"))
    assert (f.category, f.external, f.amount) == ("withdrawal", True, Decimal("-103"))


def test_interest_booked_as_deposit_is_internal(session):
    (f,) = _flows(session, make_mm(sub_type="Deposit", description="INTEREST ON CREDIT BALANCE",
                                   value=0.02, effect="Credit"))
    assert (f.category, f.external) == ("interest", False)


def test_margin_interest_booked_as_withdrawal_is_internal(session):
    (f,) = _flows(session, make_mm(sub_type="Withdrawal",
                                   description="FROM 05/16 THRU 06/15 @ 6 1/2%",
                                   value=30.9, effect="Debit"))
    assert (f.category, f.external, f.amount) == ("margin_interest", False, Decimal("-30.9"))


def test_dividend_with_symbol_is_internal_even_as_deposit(session):
    (f,) = _flows(session, make_mm(sub_type="Deposit", description="MICROSOFT CORP",
                                   symbol="MSFT", value=1.02, effect="Credit"))
    assert (f.category, f.external) == ("dividend", False)


def test_mark_to_market_is_internal(session):
    (f,) = _flows(session, make_mm(
        sub_type="Mark to Market", symbol="/MESM5",
        description="/MESM5 mark to market at 5097.25 Preliminary settlement price",
        value=773.75, effect="Debit"))
    assert (f.category, f.external) == ("futures_mtm", False)


def test_withholding_is_external(session):
    (f,) = _flows(session, make_mm(sub_type="Withdrawal", description="IRA FED WITHHOLDING",
                                   value=220.0, effect="Debit"))
    assert (f.category, f.external, f.amount) == ("withholding", True, Decimal("-220"))


def test_journal_pair_cancels_across_accounts(session):
    flows = _flows(
        session,
        make_mm(sub_type="Withdrawal", description="Journal to account 5WT84968",
                value=1980.0, effect="Debit", account="5WW28717"),
        make_mm(sub_type="Transfer", description="Journal from account 5WW28717",
                value=1980.0, effect="Credit", account="5WT84968"),
    )
    assert all(f.category == "journal" and f.external for f in flows)
    assert sum(f.amount for f in flows) == 0  # nets out portfolio-wide
    per_account = [f.amount for f in flows if f.account_number == "5WW28717"]
    assert per_account == [Decimal("-1980")]  # but is a real per-account flow


def test_acat_transfer_is_external(session):
    (f,) = _flows(session, make_mm(sub_type="Transfer", description="TRANSFER FROM INTERACTIVE B",
                                   value=23619.33, effect="Credit"))
    assert (f.category, f.external) == ("transfer", True)


def test_unknown_flow_subtype_is_flagged_but_external(session):
    (f,) = _flows(session, make_mm(sub_type="Deposit", description="MOBILE CHECK 1234",
                                   value=500.0, effect="Credit"))
    assert (f.category, f.external) == ("unclassified_flow", True)
    assert [u.txn_id for u in unclassified_flows(session)] == [f.txn_id]


def test_unknown_non_flow_subtype_is_internal_other(session):
    (f,) = _flows(session, make_mm(sub_type="Cash Merger", description="SOMETHING CORPORATE",
                                   value=10.0, effect="Credit"))
    assert (f.category, f.external) == ("other", False)


def test_account_and_date_filters(session):
    _flows(
        session,
        make_mm(sub_type="Deposit", description="ACH DEPOSIT", value=100.0,
                effect="Credit", account="A1", executed_at="2026-01-10T12:00:00+00:00"),
        make_mm(sub_type="Deposit", description="ACH DEPOSIT", value=200.0,
                effect="Credit", account="A2", executed_at="2026-02-10T12:00:00+00:00"),
    )
    assert [f.amount for f in external_flows(session, account="A1")] == [Decimal("100")]
    assert [f.amount for f in external_flows(session, start=date(2026, 2, 1))] == [Decimal("200")]
    assert external_flows(session, end=date(2026, 1, 1)) == []
