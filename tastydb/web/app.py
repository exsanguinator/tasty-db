"""Read-only web dashboard over the tastydb database.

The only write path is POST /marks/refresh, which caches fresh market marks
for unrealized PnL. Sync/process stay in the CLI.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..analytics import (
    list_closes,
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


def create_app(config: Config) -> FastAPI:
    engine = make_engine(config.resolved_db_url)
    init_db(engine)
    session_factory = make_session_factory(engine)

    app = FastAPI(title="tasty-db dashboard", version=__version__)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")
    templates.env.filters["money"] = _money
    templates.env.filters["signed_money"] = _signed_money
    templates.env.filters["pnl_class"] = _pnl_class
    templates.env.filters["qty"] = _qty
    templates.env.filters["pct"] = _pct

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

    @app.get("/")
    def overview(request: Request):
        f = filters_from(request)
        with session_factory() as session:
            by_underlying = realized_pnl(session, **f, group_by="underlying")
            by_reason = realized_pnl(session, **f, group_by="close_reason")
            points = realized_timeseries(session, **f)
            has_lots = session.execute(select(Lot.lot_id).limit(1)).first() is not None
        total = sum((r.realized_pnl for r in by_underlying), Decimal("0"))
        fees = sum((r.fees for r in by_underlying), Decimal("0"))
        closes = sum(r.closes for r in by_underlying)
        return render(
            request, "overview.html",
            total=total, fees=fees, closes=closes,
            winners=by_underlying[:10],
            losers=[r for r in reversed(by_underlying) if r.realized_pnl < 0][:10],
            by_reason=by_reason,
            chart_labels=[p[0].isoformat() for p in points],
            chart_values=[float(p[2]) for p in points],
            has_lots=has_lots,
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
            msg = "no API credentials — set TT_CLIENT_SECRET / TT_REFRESH_TOKEN"
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
    def performance_view(request: Request):
        f = filters_from(request)
        with session_factory() as session:
            pr = period_returns(session, **f)
            nlv = nlv_series(session, **f)
            index = twr_index(session, **f)
            flows = external_flows(session, **f)
        flows.sort(key=lambda fl: (fl.date, fl.txn_id), reverse=True)
        return render(
            request, "performance.html",
            pr=pr, flows=flows,
            nlv_labels=[p[0].isoformat() for p in nlv],
            nlv_values=[float(p[1]) for p in nlv],
            idx_labels=[p[0].isoformat() for p in index],
            idx_values=[float(p[1]) for p in index],
        )

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

    @app.get("/underlying/{symbol}")
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

    return app
