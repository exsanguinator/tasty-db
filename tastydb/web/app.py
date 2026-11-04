"""Web dashboard over the tastydb database.

Report pages are read-only. The write paths are POST /marks/refresh (caches
fresh market marks for unrealized PnL) and the Database page's
POST /database/{sync,process,accounts/refresh}, which run the same `ops`
functions as `tastydb sync` / `process` / `accounts`. Sync and process run in
one background thread at a time (`JobRunner`) so a long backfill never holds
an HTTP request open and two rebuilds can never overlap.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..analytics import (
    credits_collected,
    credits_timeseries,
    list_closes,
    list_credit_transactions,
    open_positions,
    realized_pnl,
    realized_timeseries,
    strategies,
)
from ..auth import AuthError
from ..cashflows import external_flows
from ..chains import chain_detail, chains
from ..client import TastyClient
from ..config import Config
from ..db import init_db, make_engine, make_session_factory
from ..marks import refresh_marks
from ..models import Account, Lot, LotClose
from ..ops import db_status, list_accounts, process_summary, run_process, run_sync
from ..returns import nlv_series, period_returns, twr_index
from .. import __version__

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent


def _money(value) -> str:
    if value is None:
        return "—"
    v = Decimal(str(value))
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _signed_money(value) -> str:
    if value is None:
        return "—"
    v = Decimal(str(value))
    return ("+" if v > 0 else "") + _money(v)


def _pnl_class(value) -> str:
    if value is None:
        return ""
    v = Decimal(str(value))
    return "pos" if v > 0 else "neg" if v < 0 else ""


def _qty(value) -> str:
    v = Decimal(str(value))
    return f"{v.normalize():f}"


def _pct(value) -> str:
    if value is None:
        return "—"
    return f"{Decimal(str(value)) * 100:+.2f}%"


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


NO_CREDENTIALS = "no API credentials — set TT_CLIENT_SECRET / TT_REFRESH_TOKEN"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Job:
    kind: str  # "sync & process" | "process"
    status: str = "running"  # running | done | failed
    stage: str = "starting"
    started_at: datetime = field(default_factory=_utcnow)
    finished_at: datetime | None = None
    log: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "log": list(self.log),
            "error": self.error,
        }


class JobRunner:
    """Runs at most one maintenance job at a time in a background thread and
    keeps the last job's state for the Database page."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.job: Job | None = None

    @property
    def running(self) -> bool:
        return self.job is not None and self.job.status == "running"

    def start(self, kind: str, work) -> bool:
        """Start `work(job)` in the background; False if a job is running."""
        with self._lock:
            if self.running:
                return False
            job = self.job = Job(kind)
            self._thread = threading.Thread(
                target=self._run, args=(job, work), name="tastydb-job", daemon=True
            )
            self._thread.start()
            return True

    @staticmethod
    def _run(job: Job, work) -> None:
        try:
            work(job)
            job.status = "done"
            job.stage = "finished"
        except AuthError:
            job.status, job.error = "failed", NO_CREDENTIALS
        except Exception as exc:  # noqa: BLE001 - surface on the page, never kill the server
            log.exception("%s job failed", job.kind)
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = _utcnow()

    def wait(self, timeout: float | None = None) -> None:
        """Block until the current job finishes (tests)."""
        if self._thread is not None:
            self._thread.join(timeout)


def create_app(config: Config) -> FastAPI:
    engine = make_engine(config.resolved_db_url)
    init_db(engine)
    session_factory = make_session_factory(engine)

    app = FastAPI(title="tasty-db dashboard", version=__version__)
    jobs = app.state.jobs = JobRunner()
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")
    templates.env.filters["money"] = _money
    templates.env.filters["signed_money"] = _signed_money
    templates.env.filters["pnl_class"] = _pnl_class
    templates.env.filters["qty"] = _qty
    templates.env.filters["pct"] = _pct
    templates.env.globals["timedelta"] = timedelta

    def filters_from(request: Request) -> dict:
        params = request.query_params
        return {
            "account": params.get("account") or None,
            "start": _parse_date(params.get("start")),
            "end": _parse_date(params.get("end")),
        }

    def render(request: Request, template: str, **context):
        filters = filters_from(request)
        with session_factory() as session:
            accounts = session.execute(
                select(Account).order_by(Account.account_number)
            ).scalars().all()
        query = {
            k: (v.isoformat() if isinstance(v, date) else v)
            for k, v in filters.items() if v
        }
        context.update(
            request=request,
            filters=filters,
            filter_query=urlencode(query),
            accounts=accounts,
            account_names={a.account_number: a.nickname or a.account_number for a in accounts},
            today=date.today(),
            message=request.query_params.get("msg"),
        )
        return templates.TemplateResponse(request, template, context)

    def group_choice(request: Request) -> str:
        """`?group=strategy` switches a by-underlying panel to by-strategy."""
        return "strategy" if request.query_params.get("group") == "strategy" else "underlying"

    @app.get("/")
    def overview(request: Request):
        f = filters_from(request)
        with session_factory() as session:
            pr = period_returns(session, **f)
            nlv = nlv_series(session, **f)
            index = twr_index(session, **f)
            flows = external_flows(session, **f)
        flows.sort(key=lambda fl: (fl.date, fl.txn_id), reverse=True)
        return render(
            request, "overview.html",
            pr=pr, flows=flows,
            nlv_labels=[p[0].isoformat() for p in nlv],
            nlv_values=[float(p[1]) for p in nlv],
            idx_labels=[p[0].isoformat() for p in index],
            idx_values=[float(p[1]) for p in index],
        )

    @app.get("/realized")
    def realized_view(request: Request):
        f = filters_from(request)
        group = group_choice(request)
        with session_factory() as session:
            rows = realized_pnl(session, **f, group_by=group)
            by_reason = realized_pnl(session, **f, group_by="close_reason")
            points = realized_timeseries(session, **f)
            has_lots = session.execute(select(Lot.lot_id).limit(1)).first() is not None
        total = sum((r.realized_pnl for r in rows), Decimal("0"))
        fees = sum((r.fees for r in rows), Decimal("0"))
        closes = sum(r.closes for r in rows)
        return render(
            request, "realized.html",
            total=total, fees=fees, closes=closes,
            rows=rows, group=group,
            by_reason=by_reason,
            chart_labels=[p[0].isoformat() for p in points],
            chart_values=[float(p[2]) for p in points],
            has_lots=has_lots,
        )

    @app.get("/credits")
    def credits_page(request: Request):
        f = filters_from(request)
        group = group_choice(request)
        with session_factory() as session:
            rows = credits_collected(session, **f, group_by=group)
            points = credits_timeseries(session, **f)
        total = sum((r.credits for r in rows), Decimal("0"))
        trades = sum(r.trades for r in rows)
        return render(
            request, "credits.html",
            total=total, trades=trades, rows=rows, group=group,
            chart_labels=[p[0].isoformat() for p in points],
            chart_values=[float(p[2]) for p in points],
        )

    @app.get("/closes")
    def closes_view(request: Request, underlying: str | None = None, page: int = 1):
        f = filters_from(request)
        page_size = 100
        with session_factory() as session:
            if request.query_params.get("group") == "underlying":
                grouped = realized_pnl(session, **f, underlying=underlying)
                return render(request, "closes.html", grouped=grouped,
                              underlying=underlying, rows=None, total=0, page=1, pages=1)
            rows, total = list_closes(
                session, **f, underlying=underlying,
                limit=page_size, offset=(page - 1) * page_size,
            )
        return render(
            request, "closes.html",
            rows=rows, total=total, grouped=None, underlying=underlying,
            page=page, pages=max(1, -(-total // page_size)),
        )

    @app.get("/positions")
    def positions_view(request: Request):
        f = filters_from(request)
        with session_factory() as session:
            rows = open_positions(session, account=f["account"])
        total_unrealized = sum(
            (r.unrealized_pnl for r in rows if r.unrealized_pnl is not None), Decimal("0")
        )
        marked = [r for r in rows if r.unrealized_pnl is not None]
        stalest = min((r.mark_updated_at for r in marked), default=None)
        return render(
            request, "positions.html",
            rows=rows, total_unrealized=total_unrealized,
            marked_count=len(marked), stalest=stalest,
            can_refresh=config.has_credentials,
        )

    @app.post("/marks/refresh")
    def marks_refresh(request: Request, back: str = Form("/positions")):
        try:
            client = TastyClient(config)
            with session_factory() as session:
                updated = refresh_marks(session, client)
            msg = f"refreshed marks for {updated} symbols"
        except AuthError:
            msg = NO_CREDENTIALS
        except Exception as exc:  # noqa: BLE001 - surface, don't 500
            log.exception("marks refresh failed")
            msg = f"marks refresh failed: {exc}"
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}msg={msg}", status_code=303)

    @app.get("/strategies")
    def strategies_view(request: Request, underlying: str | None = None):
        f = filters_from(request)
        with session_factory() as session:
            rows = strategies(session, **f, underlying=underlying, limit=200)
        return render(request, "strategies.html", rows=rows, underlying=underlying)

    @app.get("/chains")
    def chains_view(request: Request, underlying: str | None = None):
        f = filters_from(request)
        with session_factory() as session:
            rows = chains(session, **f, underlying=underlying, limit=200)
        return render(request, "chains.html", rows=rows, underlying=underlying)

    @app.get("/chain/{chain_id}")
    def chain_view(request: Request, chain_id: int):
        with session_factory() as session:
            details = chain_detail(session, chain_id)
        return render(request, "chain.html", chain_id=chain_id, details=details)

    @app.get("/performance")
    def performance_redirect(request: Request):
        """Old URL of the Overview page; keeps bookmarks working."""
        query = request.url.query
        return RedirectResponse(f"/?{query}" if query else "/", status_code=301)

    @app.get("/lot/{lot_id}")
    def lot_view(request: Request, lot_id: int):
        with session_factory() as session:
            lot = session.get(Lot, lot_id)
            if lot is None:
                return render(request, "lot.html", lot=None, closes=[],
                              inbound_links=[], siblings=[])
            closes = session.execute(
                select(LotClose).where(LotClose.lot_id == lot_id)
                .order_by(LotClose.close_date)
            ).scalars().all()
            inbound = session.execute(
                select(LotClose).where(LotClose.linked_lot_id == lot_id)
            ).scalars().all()
            siblings = []
            if lot.open_order_id is not None:
                siblings = session.execute(
                    select(Lot).where(
                        Lot.open_order_id == lot.open_order_id,
                        Lot.lot_id != lot.lot_id,
                    )
                ).scalars().all()
        return render(request, "lot.html", lot=lot, closes=closes,
                      inbound_links=inbound, siblings=siblings)

    @app.get("/underlying/{symbol:path}")  # :path — crypto symbols contain a slash (SOL/USD)
    def underlying_view(request: Request, symbol: str):
        f = filters_from(request)
        with session_factory() as session:
            rows, total = list_closes(session, **f, underlying=symbol, limit=200)
            strats = strategies(session, **f, underlying=symbol, limit=50)
            positions = [
                p for p in open_positions(session, account=f["account"])
                if p.underlying_symbol == symbol
            ]
            points = realized_timeseries(session, **f, underlying=symbol)
        realized = sum((r.realized_pnl for r in rows), Decimal("0"))
        return render(
            request, "underlying.html",
            symbol=symbol, rows=rows, total=total, strats=strats,
            positions=positions, realized=realized,
            chart_labels=[p[0].isoformat() for p in points],
            chart_values=[float(p[2]) for p in points],
        )

    @app.get("/strategy/{name}")
    def strategy_view(request: Request, name: str):
        """Drill-down for one strategy name: the trades that carry it."""
        f = filters_from(request)
        with session_factory() as session:
            by_underlying = realized_pnl(session, **f, strategy=name)
            strats = strategies(session, **f, strategy=name, limit=200)
            points = realized_timeseries(session, **f, strategy=name)
            credit_rows, credit_count, credit_total = list_credit_transactions(
                session, **f, strategy=name, limit=200,
            )
        return render(
            request, "strategy.html",
            name=name, rows=strats, by_underlying=by_underlying,
            realized=sum((r.realized_pnl for r in by_underlying), Decimal("0")),
            fees=sum((r.fees for r in by_underlying), Decimal("0")),
            closes=sum(r.closes for r in by_underlying),
            credit_rows=credit_rows, credit_count=credit_count,
            credit_total=credit_total,
            chart_labels=[p[0].isoformat() for p in points],
            chart_values=[float(p[2]) for p in points],
        )

    # ------------------------------------------------------------ database

    def back_to_database(msg: str) -> RedirectResponse:
        return RedirectResponse(f"/database?{urlencode({'msg': msg})}", status_code=303)

    def process_step(job: Job, session, method: str, offline_meta: bool) -> None:
        job.stage = "processing"
        client = None
        if not offline_meta and config.has_credentials:
            try:
                client = TastyClient(config)
            except AuthError:
                client = None
        if client is None:
            job.log.append("instrument metadata: cache + symbology fallbacks (no API)")
        stats = run_process(session, client, method=method,
                            grace_days=config.expiration_grace_days)
        job.log.append(process_summary(stats))

    def clean_method(raw: str | None) -> str:
        return raw if raw in ("fifo", "lifo") else config.match_method

    @app.get("/database")
    def database_view(request: Request):
        with session_factory() as session:
            status = db_status(session, with_activity=True)
            cached = session.execute(
                select(Account).order_by(Account.account_number)
            ).scalars().all()
            numbers = [a.account_number for a in cached]
            numbers += sorted(set(status.activity) - set(numbers))
        return render(
            request, "database.html",
            status=status, account_rows=cached, account_numbers=numbers,
            db_url=config.resolved_db_url, env=config.env,
            match_method=config.match_method,
            can_sync=config.has_credentials, job=jobs.job, running=jobs.running,
        )

    @app.get("/database/job")
    def database_job():
        return JSONResponse(jobs.job.as_dict() if jobs.job else {"status": "idle"})

    @app.post("/database/sync")
    def database_sync(
        account: str = Form(""),
        backfill: bool = Form(False),
        since: str = Form(""),
        method: str = Form(""),
        offline_meta: bool = Form(False),
    ):
        if not config.has_credentials:
            return back_to_database(NO_CREDENTIALS)
        since_date = _parse_date(since)
        method = clean_method(method)

        def work(job: Job) -> None:
            client = TastyClient(config)
            with session_factory() as session:
                run_sync(
                    session, client, account=account or None,
                    backfill=backfill, since=since_date,
                    progress=lambda stage: setattr(job, "stage", stage),
                    on_result=lambda r: job.log.extend(
                        [r.transactions_line, r.snapshots_line]
                    ),
                )
                process_step(job, session, method, offline_meta)

        if not jobs.start("sync & process", work):
            return back_to_database("a job is already running")
        return back_to_database("sync & process started")

    @app.post("/database/process")
    def database_process(method: str = Form(""), offline_meta: bool = Form(False)):
        method = clean_method(method)

        def work(job: Job) -> None:
            with session_factory() as session:
                process_step(job, session, method, offline_meta)

        if not jobs.start("process", work):
            return back_to_database("a job is already running")
        return back_to_database("process started")

    @app.post("/database/accounts/refresh")
    def database_accounts_refresh():
        if not config.has_credentials:
            return back_to_database(NO_CREDENTIALS)
        try:
            with session_factory() as session:
                rows, _ = list_accounts(session, TastyClient(config))
            msg = f"refreshed {len(rows)} accounts from the API"
        except AuthError:
            msg = NO_CREDENTIALS
        except Exception as exc:  # noqa: BLE001 - surface, don't 500
            log.exception("accounts refresh failed")
            msg = f"accounts refresh failed: {exc}"
        return back_to_database(msg)

    return app
