"""tastydb command-line interface.

    tastydb init-db                        create tables
    tastydb accounts                       list account numbers (API)
    tastydb sync [--backfill] [--since D]  pull transactions into raw storage
    tastydb process [--method lifo]        classify + match into lots/closes
    tastydb pnl --start D --end D          realized PnL report
    tastydb status                         ingest/processing overview
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from decimal import Decimal

import click
from sqlalchemy import func, select

from .analytics import realized_pnl
from .auth import AuthError
from .client import TastyClient
from .config import Config, load_dotenv
from .db import init_db, make_engine, make_session_factory
from .ingest import sync_account, upsert_accounts
from .instruments import MetaProvider
from .matching import rebuild_lots
from .models import Account, Lot, LotClose, ProcessingStatus, RawTransaction

log = logging.getLogger(__name__)


class App:
    def __init__(self, config: Config):
        self.config = config
        self.engine = make_engine(config.resolved_db_url)
        self.session_factory = make_session_factory(self.engine)
        self._client: TastyClient | None = None

    def client(self) -> TastyClient:
        if self._client is None:
            self._client = TastyClient(self.config)
        return self._client

    def optional_client(self) -> TastyClient | None:
        """A client if credentials exist, else None (offline degradation)."""
        if not self.config.has_credentials:
            return None
        try:
            return self.client()
        except AuthError:
            return None


@click.group()
@click.option("--db", "db_url", default=None,
              help="SQLAlchemy DB URL (default: $TASTYDB_DB_URL, else per-environment: "
                   "sqlite:///tastydb.sqlite3 for prod, sqlite:///tastydb-sandbox.sqlite3 for sandbox)")
@click.option("--env-file", default=".env", show_default=True,
              help="Load environment variables from this file (real env vars win)")
@click.option("--sandbox", is_flag=True, help="Use the certification/sandbox environment")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging")
@click.pass_context
def main(ctx: click.Context, db_url: str | None, env_file: str, sandbox: bool, verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    applied = load_dotenv(env_file)
    if applied:
        log.debug("loaded %d variables from %s", len(applied), env_file)
    config = Config()
    if db_url:
        config.db_url = db_url
    if sandbox:
        config.env = "sandbox"
    ctx.obj = App(config)


@main.command("init-db")
@click.pass_obj
def init_db_cmd(app: App):
    """Create database tables."""
    init_db(app.engine)
    click.echo(f"initialized {app.config.resolved_db_url}")


@main.command()
@click.pass_obj
def accounts(app: App):
    """List accounts visible to your OAuth grant (cached copy when offline)."""
    init_db(app.engine)
    client = app.optional_client()
    with app.session_factory() as session:
        if client is not None:
            rows = client.accounts()
            upsert_accounts(session, rows)
            session.commit()
            listing = [(a["account-number"], a.get("nickname") or "") for a in rows]
        else:
            cached = session.execute(
                select(Account).order_by(Account.account_number)
            ).scalars().all()
            if not cached:
                raise click.ClickException(
                    "no credentials and no cached accounts — run `tastydb sync` once"
                )
            click.echo("(offline: cached account list)", err=True)
            listing = [(a.account_number, a.nickname or "") for a in cached]
    for number, nickname in listing:
        click.echo(f"{number:<12} {nickname}".rstrip())


@main.command()
@click.option("--account", "account_number", default=None, help="Sync one account (default: all)")
@click.option("--backfill", is_flag=True, help="Fetch full history instead of incremental")
@click.option("--since", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Explicit start date (overrides both modes)")
@click.pass_obj
def sync(app: App, account_number: str | None, backfill: bool, since):
    """Pull transaction history into raw storage (idempotent, safe to re-run)."""
    init_db(app.engine)
    client = app.client()
    account_rows = client.accounts()
    numbers = [account_number] if account_number else [
        a["account-number"] for a in account_rows
    ]
    since_date: date | None = since.date() if since else None
    with app.session_factory() as session:
        upsert_accounts(session, account_rows)
        for number in numbers:
            run = sync_account(session, client, number, backfill=backfill, since=since_date)
            click.echo(
                f"{number}: {run.mode} from {run.start_date_used or 'beginning'} — "
                f"fetched {run.fetched}, inserted {run.inserted}, updated {run.updated}"
            )
    click.echo("run `tastydb process` to rebuild lots")


@main.command()
@click.option("--method", type=click.Choice(["fifo", "lifo"]), default=None,
              help="Lot matching order (default: $TASTYDB_MATCH_METHOD or fifo)")
@click.option("--offline", is_flag=True,
              help="Don't call the API for instrument metadata; use cache + symbology fallbacks")
@click.pass_obj
def process(app: App, method: str | None, offline: bool):
    """Classify raw transactions and rebuild lots/closes (derived tables only)."""
    init_db(app.engine)
    client = None if offline else app.optional_client()
    if client is None and not offline:
        click.echo("note: no API credentials; instrument metadata will use fallbacks", err=True)
    with app.session_factory() as session:
        meta = MetaProvider(session, client)
        stats = rebuild_lots(
            session,
            meta,
            method=method or app.config.match_method,
            grace_days=app.config.expiration_grace_days,
        )
    click.echo(
        f"{stats['transactions']} transactions -> {stats['events']} events, "
        f"{stats['closes']} closes, {stats['open_lots']} open lots "
        f"({stats['expired_worthless']} swept as worthless expiration)"
    )


@main.command()
@click.option("--start", type=click.DateTime(formats=["%Y-%m-%d"]), default=None)
@click.option("--end", type=click.DateTime(formats=["%Y-%m-%d"]), default=None)
@click.option("--underlying", default=None, help="Filter to one underlying symbol")
@click.option("--account", default=None, help="Filter to one account")
@click.option("--group-by", type=click.Choice(["underlying", "asset_type", "close_reason"]),
              default="underlying")
@click.pass_obj
def pnl(app: App, start, end, underlying: str | None, account: str | None, group_by: str):
    """Realized PnL summed from lot closes over a date range."""
    with app.session_factory() as session:
        rows = realized_pnl(
            session,
            start=start.date() if start else None,
            end=end.date() if end else None,
            underlying=underlying,
            account=account,
            group_by=group_by,
        )
    if not rows:
        click.echo("no realized closes in range")
        return
    header = f"{group_by:<20} {'closes':>7} {'qty':>10} {'fees':>12} {'realized pnl':>14}"
    click.echo(header)
    click.echo("-" * len(header))
    total_fees = total_pnl = Decimal("0")
    for row in rows:
        click.echo(
            f"{row.group:<20} {row.closes:>7} {row.quantity_closed:>10.2f} "
            f"{row.fees:>12.2f} {row.realized_pnl:>14.2f}"
        )
        total_fees += row.fees
        total_pnl += row.realized_pnl
    click.echo("-" * len(header))
    click.echo(f"{'TOTAL':<20} {'':>7} {'':>10} {total_fees:>12.2f} {total_pnl:>14.2f}")


@main.command()
@click.pass_obj
def status(app: App):
    """Counts of raw transactions by processing status, plus lot totals."""
    init_db(app.engine)
    with app.session_factory() as session:
        rows = session.execute(
            select(RawTransaction.processing_status, func.count())
            .group_by(RawTransaction.processing_status)
        ).all()
        open_lots = session.execute(
            select(func.count()).select_from(Lot).where(Lot.remaining_quantity > 0)
        ).scalar_one()
        closes = session.execute(select(func.count()).select_from(LotClose)).scalar_one()

        click.echo("raw transactions:")
        for status_value, count in rows:
            click.echo(f"  {status_value.value:<12} {count}")
        if not rows:
            click.echo("  (none — run `tastydb sync`)")
        click.echo(f"open lots: {open_lots}")
        click.echo(f"lot closes: {closes}")

        problems = session.execute(
            select(RawTransaction)
            .where(RawTransaction.processing_status.in_(
                [ProcessingStatus.unsupported, ProcessingStatus.error]
            ))
            .order_by(RawTransaction.executed_at)
            .limit(20)
        ).scalars().all()
        if problems:
            click.echo("\nneeds attention (first 20):")
            for txn in problems:
                click.echo(
                    f"  {txn.id} {txn.executed_at:%Y-%m-%d} {txn.symbol or '-'} "
                    f"[{txn.transaction_type}/{txn.transaction_sub_type}] {txn.processing_note}"
                )


if __name__ == "__main__":
    main()
