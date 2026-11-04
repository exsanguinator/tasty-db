"""Phase 4: strategy grouping, marks/unrealized PnL, derived-schema migration,
and dashboard smoke tests."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from tastydb.analytics import open_positions, realized_timeseries, strategies
from tastydb.db import init_db
from tastydb.models import Lot, Mark

from .conftest import make_txn, run_pipeline


def _vertical_payloads():
    """A 2-leg XSP call vertical opened as one order, closed as another."""
    return [
        make_txn(txn_id=901, action="Sell to Open", symbol="XSP   260408C00658000",
                 underlying="XSP", instrument_type="Equity Option", quantity=2,
                 price=4.42, executed_at="2026-04-08T14:00:00+00:00",
                 **{"order-id": 70001, "leg-count": 2}),
        make_txn(txn_id=902, action="Buy to Open", symbol="XSP   260408C00659000",
                 underlying="XSP", instrument_type="Equity Option", quantity=2,
                 price=3.92, executed_at="2026-04-08T14:00:00+00:00",
                 **{"order-id": 70001, "leg-count": 2}),
        make_txn(txn_id=903, action="Buy to Close", symbol="XSP   260408C00658000",
                 underlying="XSP", instrument_type="Equity Option", quantity=2,
                 price=1.00, executed_at="2026-04-08T18:00:00+00:00",
                 **{"order-id": 70002, "leg-count": 2}),
        make_txn(txn_id=904, action="Sell to Close", symbol="XSP   260408C00659000",
                 underlying="XSP", instrument_type="Equity Option", quantity=2,
                 price=0.60, executed_at="2026-04-08T18:00:00+00:00",
                 **{"order-id": 70002, "leg-count": 2}),
    ]


def test_order_ids_propagate_to_lots_and_closes(session):
    run_pipeline(session, _vertical_payloads())
    lots = session.execute(select(Lot)).scalars().all()
    assert {l.open_order_id for l in lots} == {70001}
    closes = [c for l in lots for c in l.closes]
    assert {c.open_order_id for c in closes} == {70001}
    assert {c.close_order_id for c in closes} == {70002}


def test_strategies_group_legs_of_one_order(session):
    run_pipeline(session, _vertical_payloads())
    rows = strategies(session)
    assert len(rows) == 1
    strat = rows[0]
    assert strat.open_order_id == 70001
    assert len(strat.legs) == 2
    # short leg (4.42-1.00)*2*100=684, long leg (0.60-3.92)*2*100=-664
    assert strat.realized_pnl == Decimal("20")
    assert strat.underlying_symbol == "XSP"
    assert sorted(l.side.value for l in strat.legs) == ["long", "short"]
    # short 658 call / long 659 call: premium collected
    assert strat.strategy_name == "Call credit spread"


def test_strategies_fall_back_to_single_lot_without_order(session):
    # worthless-expiration sweep close has no closing order and the lot came
    # from a normal order; grouping still keys on the OPENING order id
    run_pipeline(session, [
        make_txn(txn_id=911, action="Buy to Open", symbol="AAPL", quantity=10,
                 price=10.0, executed_at="2024-01-02T15:00:00+00:00",
                 **{"order-id": 70010}),
        make_txn(txn_id=912, action="Sell to Close", symbol="AAPL", quantity=10,
                 price=12.0, executed_at="2024-02-02T15:00:00+00:00",
                 **{"order-id": 70011}),
    ])
    rows = strategies(session)
    assert len(rows) == 1
    assert rows[0].open_order_id == 70010


def test_open_positions_and_unrealized_marks(session):
    run_pipeline(session, [
        make_txn(action="Buy to Open", symbol="AAPL", quantity=60, price=10.0,
                 executed_at="2024-01-02T15:00:00+00:00", commission=2.0),
        make_txn(action="Buy to Open", symbol="AAPL", quantity=40, price=12.0,
                 executed_at="2024-01-03T15:00:00+00:00"),
        make_txn(action="Sell to Open", symbol="SPY   270119P00450000",
                 underlying="SPY", instrument_type="Equity Option", quantity=1,
                 price=5.0, executed_at="2024-01-02T15:00:00+00:00"),
    ])
    rows = open_positions(session)
    assert len(rows) == 2  # marks not refreshed yet
    aapl = next(r for r in rows if r.symbol == "AAPL")
    assert aapl.quantity == 100
    assert aapl.avg_open_price == Decimal("10.8")  # (60*10 + 40*12) / 100
    assert aapl.cost_basis == Decimal("1080")
    assert aapl.open_fees == Decimal("2")
    assert aapl.unrealized_pnl is None

    session.add(Mark(symbol="AAPL", mark=Decimal("11.5"), updated_at=datetime(2026, 7, 4)))
    session.add(Mark(symbol="SPY   270119P00450000", mark=Decimal("7.0"),
                     updated_at=datetime(2026, 7, 4)))
    session.commit()

    rows = open_positions(session)
    aapl = next(r for r in rows if r.symbol == "AAPL")
    assert aapl.unrealized_pnl == Decimal("70")  # (11.5 - 10.8) * 100
    put = next(r for r in rows if r.side.value == "short")
    assert put.unrealized_pnl == Decimal("-200")  # short: (5 - 7) * 100


def test_realized_timeseries_is_cumulative(session):
    run_pipeline(session, [
        make_txn(action="Buy to Open", symbol="AAPL", quantity=100, price=10.0,
                 executed_at="2024-01-02T15:00:00+00:00"),
        make_txn(action="Sell to Close", symbol="AAPL", quantity=50, price=12.0,
                 executed_at="2024-02-01T15:00:00+00:00"),
        make_txn(action="Sell to Close", symbol="AAPL", quantity=50, price=9.0,
                 executed_at="2024-03-01T15:00:00+00:00"),
    ])
    points = realized_timeseries(session)
    assert [(str(d), float(cum)) for d, _, cum in points] == [
        ("2024-02-01", 100.0), ("2024-03-01", 50.0),
    ]


def test_derived_schema_change_triggers_rebuildable_drop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/x.sqlite3")
    init_db(engine)
    with engine.begin() as conn:  # simulate an old-shape derived table
        conn.execute(text("ALTER TABLE lots ADD COLUMN legacy_junk TEXT"))
    with Session(engine) as s:
        s.execute(text("INSERT INTO raw_transactions (id, account_number, executed_at, payload, processing_status, total_fees, ingested_at) VALUES (1,'A','2024-01-01', '{}', 'pending', 0, '2024-01-01')"))
        s.commit()
    init_db(engine)  # detects column mismatch, drops + recreates
    cols = {c["name"] for c in inspect(engine).get_columns("lots")}
    assert "open_order_id" in cols
    with Session(engine) as s:  # raw data untouched
        assert s.execute(text("SELECT COUNT(*) FROM raw_transactions")).scalar_one() == 1


def _dashboard_client(session_payloads, tmp_path):
    from fastapi.testclient import TestClient

    from tastydb.config import Config
    from tastydb.db import make_session_factory
    from tastydb.ingest import ingest_payloads, upsert_accounts
    from tastydb.instruments import MetaProvider
    from tastydb.matching import rebuild_lots
    from tastydb.web.app import create_app

    config = Config(db_url=f"sqlite:///{tmp_path}/dash.sqlite3")
    config.client_secret = config.refresh_token = None
    app = create_app(config)
    from tastydb.db import make_engine
    engine = make_engine(config.db_url)
    with Session(engine) as s:
        ingest_payloads(s, session_payloads)
        upsert_accounts(s, [{"account-number": "5WT00001", "nickname": "Test IRA"}])
        s.commit()
        rebuild_lots(s, MetaProvider(s, client=None))
    return TestClient(app)


def test_dashboard_routes_smoke(tmp_path):
    client = _dashboard_client(_vertical_payloads(), tmp_path)

    home = client.get("/realized")
    assert home.status_code == 200
    assert "Realized PnL" in home.text
    assert "XSP" in home.text
    assert "By underlying" in home.text
    assert "Test IRA" in home.text  # account dropdown uses nickname

    closes = client.get("/closes")
    assert closes.status_code == 200
    assert "XSP   260408C00658000" in closes.text

    grouped = client.get("/closes?group=underlying")
    assert grouped.status_code == 200

    positions = client.get("/positions")
    assert positions.status_code == 200

    strategies_page = client.get("/strategies")
    assert strategies_page.status_code == 200
    assert "short 2" in strategies_page.text
    assert "Call credit spread" in strategies_page.text

    lot_page = client.get("/lot/901")
    assert lot_page.status_code == 200
    assert "Lot 901" in lot_page.text
    assert "70001" in lot_page.text  # opening order + sibling leg linkage
    assert "/lot/902" in lot_page.text

    underlying_page = client.get("/underlying/XSP")
    assert underlying_page.status_code == 200
    assert "Cumulative realized PnL" in underlying_page.text
    # crypto underlyings carry a slash, so the route takes the rest of the path
    assert client.get("/underlying/SOL/USD").status_code == 200

    credits_page = client.get("/credits")
    assert credits_page.status_code == 200
    assert "Cumulative credits" in credits_page.text
    assert '"labels": ["' in credits_page.text  # chart has data points

    by_strategy = client.get("/realized?group=strategy")
    assert by_strategy.status_code == 200
    assert "/strategy/Call%20credit%20spread" in by_strategy.text
    credits_by_strategy = client.get("/credits?group=strategy")
    assert "/strategy/Call%20credit%20spread?#credits" in credits_by_strategy.text

    strategy_page = client.get("/strategy/Call credit spread")
    assert strategy_page.status_code == 200
    assert "XSP   260408C00658000" in strategy_page.text  # the trade's legs
    assert "/lot/901" in strategy_page.text
    assert "Sell to Open" in strategy_page.text  # credits panel lists the trades

    assert client.get("/lot/999999").status_code == 200  # not-found page, no 500
    assert client.get("/strategy/No%20such%20thing").status_code == 200


def test_dashboard_filters_narrow_results(tmp_path):
    payloads = _vertical_payloads() + [
        make_txn(txn_id=950, action="Buy to Open", symbol="MSFT", quantity=10,
                 price=100.0, executed_at="2023-06-01T15:00:00+00:00"),
        make_txn(txn_id=951, action="Sell to Close", symbol="MSFT", quantity=10,
                 price=110.0, executed_at="2023-06-15T15:00:00+00:00"),
    ]
    client = _dashboard_client(payloads, tmp_path)
    all_rows = client.get("/closes")
    assert "MSFT" in all_rows.text and "XSP" in all_rows.text
    only_2026 = client.get("/closes?start=2026-01-01")
    assert "MSFT" not in only_2026.text and "XSP" in only_2026.text

    marks_refresh = client.post("/marks/refresh", data={"back": "/positions"}, follow_redirects=True)
    assert marks_refresh.status_code == 200
    assert "no API credentials" in marks_refresh.text


# ---------------------------------------------------------------- Database page


class _FakeSyncClient:
    """Stands in for TastyClient: serves canned payloads, no network."""

    def __init__(self, payloads, snapshots_fail=False):
        self.payloads = payloads
        self.snapshots_fail = snapshots_fail

    def accounts(self):
        return [{"account-number": "5WT00001", "nickname": "Test IRA",
                 "account-type-name": "Roth IRA", "margin-or-cash": "Cash"}]

    def iter_transactions(self, account_number, start_date=None):
        return iter(self.payloads)

    def iter_balance_snapshots(self, account_number, start_date=None):
        from tastydb.client import ApiError
        if self.snapshots_fail:
            raise ApiError(503, "snapshots unavailable")
        return iter([])

    def net_liq_history(self, account_number, time_back="all"):
        return []


def _database_app(tmp_path, monkeypatch=None, fake=None):
    """A dashboard over an empty DB; with `fake`, credentials are set and the
    web module's TastyClient is replaced by it."""
    from fastapi.testclient import TestClient

    from tastydb.config import Config
    from tastydb.web import app as web_app

    config = Config(db_url=f"sqlite:///{tmp_path}/db-page.sqlite3")
    config.client_secret = config.refresh_token = None
    if fake is not None:
        config.client_secret, config.refresh_token = "secret", "token"
        monkeypatch.setattr(web_app, "TastyClient", lambda _config: fake)
    app = web_app.create_app(config)
    return TestClient(app), app.state.jobs, config


def test_database_page_shows_accounts_and_status(tmp_path):
    client = _dashboard_client(_vertical_payloads(), tmp_path)
    page = client.get("/database")
    assert page.status_code == 200
    assert 'href="/database"' in page.text  # nav link
    assert "Test IRA" in page.text and "5WT00001" in page.text
    assert "Processing status" in page.text
    assert "processed" in page.text
    # sync needs credentials; process-only does not
    assert 'disabled title="no API credentials' in page.text
    assert "Process only" in page.text
    assert 'name="start"' not in page.text  # report filter bar hidden here


def test_database_sync_without_credentials_does_not_start(tmp_path):
    client, jobs, _ = _database_app(tmp_path)
    resp = client.post("/database/sync", follow_redirects=True)
    assert resp.status_code == 200
    assert "no API credentials" in resp.text
    assert jobs.job is None
    assert client.get("/database/job").json() == {"status": "idle"}
    refresh = client.post("/database/accounts/refresh", follow_redirects=True)
    assert "no API credentials" in refresh.text


def test_database_process_only_rebuilds_offline(tmp_path):
    from tastydb.db import make_engine
    from tastydb.ingest import ingest_payloads

    client, jobs, config = _database_app(tmp_path)
    engine = make_engine(config.db_url)
    with Session(engine) as s:
        ingest_payloads(s, _vertical_payloads())
        s.commit()
        assert s.execute(select(Lot)).first() is None

    resp = client.post("/database/process", data={"method": "lifo"}, follow_redirects=True)
    assert resp.status_code == 200
    jobs.wait(10)
    assert jobs.job.status == "done", jobs.job.error
    assert any("4 transactions" in line for line in jobs.job.log)
    with Session(engine) as s:
        assert len(s.execute(select(Lot)).scalars().all()) == 2
    page = client.get("/database")
    assert "Process</strong>" in page.text and "done" in page.text
    assert "setTimeout" not in page.text  # no polling once finished


def test_database_sync_and_process_with_fake_client(tmp_path, monkeypatch):
    fake = _FakeSyncClient(_vertical_payloads(), snapshots_fail=True)
    client, jobs, config = _database_app(tmp_path, monkeypatch, fake)

    resp = client.post("/database/sync", data={"backfill": "true"}, follow_redirects=True)
    assert "sync &amp; process started" in resp.text or "sync & process started" in resp.text
    jobs.wait(10)
    job = jobs.job
    assert job.status == "done", job.error
    assert job.log[0].startswith("5WT00001: backfill from beginning — fetched 4, inserted 4")
    assert "balance snapshots failed: HTTP 503" in job.log[1]
    assert job.log[-1].startswith("4 transactions ->")

    page = client.get("/database")
    assert "Roth IRA" in page.text  # accounts upserted from the API
    assert "backfill" in page.text  # last sync run listed
    assert client.get("/database/job").json()["status"] == "done"

    refresh = client.post("/database/accounts/refresh", follow_redirects=True)
    assert "refreshed 1 accounts" in refresh.text


def test_database_rejects_second_job_while_running(tmp_path, monkeypatch):
    import threading

    client, jobs, _ = _database_app(tmp_path)
    release = threading.Event()
    assert jobs.start("blocker", lambda job: release.wait(10))
    try:
        resp = client.post("/database/process", follow_redirects=True)
        assert "already running" in resp.text
        page = client.get("/database")
        assert "setTimeout" in page.text  # page polls while a job runs
        assert client.get("/database/job").json()["status"] == "running"
    finally:
        release.set()
        jobs.wait(10)
    assert jobs.job.status == "done"


def test_database_job_failure_is_reported(tmp_path):
    client, jobs, _ = _database_app(tmp_path)

    def boom(job):
        raise ValueError("kaput")

    jobs.start("process", boom)
    jobs.wait(10)
    assert jobs.job.status == "failed"
    assert "kaput" in client.get("/database").text


def test_ops_db_status_activity(tmp_path):
    from tastydb.ingest import ingest_payloads
    from tastydb.ops import db_status

    engine = create_engine(f"sqlite:///{tmp_path}/ops.sqlite3")
    init_db(engine)
    with Session(engine) as s:
        ingest_payloads(s, _vertical_payloads())
        s.commit()
        st = db_status(s, with_activity=True)
    assert st.raw_total == 4
    assert st.problem_count == 0
    act = st.activity["5WT00001"]
    assert act.transactions == 4
    assert act.first_txn == datetime(2026, 4, 8, 14, 0)
    assert act.last_sync is None and st.last_sync_at is None
