# tasty-db

**Your TastyTrade trading history, on your own machine, with real answers about
performance.**

tasty-db pulls your complete TastyTrade transaction history into a local SQLite
database and turns it into the reports the broker doesn't give you:

- **Realized PnL by lot** — every open matched to its close (FIFO or LIFO) for
  stocks, equity options, futures, and futures options, with fees allocated and
  expirations, assignments, exercises, and cash settlements handled correctly.
- **Strategy & roll-chain views** — spreads entered as one order report as one
  trade, and rolled positions are stitched into whole campaigns with running
  credit and total PnL.
- **Account-level returns** — time-weighted return (TWR) and money-weighted
  return (XIRR) computed from daily net-liq snapshots and your actual deposits
  and withdrawals, so you can compare yourself to a benchmark honestly.
- **A local web dashboard** to browse all of it — no cloud, no account linking,
  everything stays on your machine.

Syncing is idempotent and incremental: raw broker transactions are the source
of truth, and all derived tables can be rebuilt from them at any time.

## Installation

Requires Python 3.11+.

```sh
git clone https://github.com/exsanguinator/tasty-db
cd tasty-db
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

The `tastydb` command is installed into the venv (`.venv/bin/tastydb`, or just
`tastydb` after activating the venv).

## Setup: API credentials

TastyTrade's API uses OAuth2. One-time setup:

1. Log in at [my.tastytrade.com](https://my.tastytrade.com) and go to
   **Manage → My Profile → API → OAuth Applications**.
2. Create an OAuth application — this gives you a **client secret**.
3. Create a **personal grant** for it — this gives you a long-lived
   **refresh token**.

Then provide both to tasty-db, either as environment variables:

```sh
export TT_CLIENT_SECRET=...
export TT_REFRESH_TOKEN=...
```

…or in a `.env` file in the working directory (gitignored; real environment
variables take precedence):

```
TT_CLIENT_SECRET=...
TT_REFRESH_TOKEN=...
```

Optional settings: `TT_ENV=sandbox` (or `--sandbox`) to use TastyTrade's cert
environment, `TASTYDB_DB_URL` / `--db` to point at a specific database, and
`TASTYDB_MATCH_METHOD=lifo` to match lots LIFO instead of FIFO. Each
environment gets its own database file by default (`tastydb.sqlite3` for prod,
`tastydb-sandbox.sqlite3` for sandbox), so experimenting never touches real
data. Credentials are never written to the database or the repo.

## Quick start

```sh
tastydb sync --backfill   # one-time: pull full history for all accounts
tastydb process           # build lots and realized closes from the raw data
tastydb dashboard         # open the web dashboard at http://127.0.0.1:8787/
```

Day to day:

```sh
tastydb sync              # incremental pull (safe to run anytime; dedupes)
tastydb process           # rebuild derived tables after a sync
tastydb status            # health check: counts + anything needing attention
```

`sync` also records an end-of-day net-liq snapshot per account, which is what
powers the returns/performance features — so a periodic `sync` (e.g. a nightly
cron job) keeps both your trade history and your performance data current.

## Typical use cases

### "How much did I actually make on my trades?" — realized PnL

Lot-based, fee-inclusive realized PnL from actual fills:

```sh
tastydb pnl --start 2026-01-01 --end 2026-06-30   # a date window
tastydb pnl --underlying SPX                      # one underlying
tastydb pnl --group-by close_reason               # trade vs expiry vs assignment...
```

This answers the *trading skill* question: for every position you closed, what
did you make or lose? Expirations, assignments, exercises, and cash-settled
index options are all booked at broker-reported values. In the dashboard, the
**Overview**, **Closes**, **Strategies**, and **Chains** pages give the same
numbers with charts, filters, and drill-down to individual lots.

### "How is my account actually performing?" — net-liq returns

Realized trade PnL deliberately excludes dividends, interest, fees on cash,
and unrealized moves. For whole-account performance, use the returns view,
which works from daily net-liq and your external cash flows instead:

```sh
tastydb returns --start 2026-01-01                # TWR + XIRR per account
```

- **Period $PnL** = ending net-liq − starting net-liq − net deposits/withdrawals.
- **TWR** (time-weighted return) removes the effect of your deposit/withdrawal
  timing — this is the number to compare against SPY.
- **XIRR** (money-weighted return) is the annualized return on *your* dollars,
  timing included.

Deposits, withdrawals, journals, and tax withholding are classified from the
raw Money Movement history; transfers between your own accounts cancel out in
the combined view. The dashboard's **Performance** page shows the net-liq
chart, a flow-neutral growth-of-$100 chart, and the full cash-flow table.

### Comparing the two

If your realized PnL is great but your TWR is flat, the difference is living
somewhere — open positions moving against you, cash drag, or costs outside
trade fills. Running both views over the same window is the fastest way to see
where.

## The web dashboard

`tastydb dashboard` serves a read-only local web UI over the same database:

| Page | What it shows |
|---|---|
| **Overview** | Realized PnL / fees, cumulative PnL chart, best & worst underlyings |
| **Closes** | Every realized close, filterable, linked to its lot |
| **Positions** | Open lots with cost basis and unrealized PnL from cached marks |
| **Strategies** | Multi-leg orders (spreads, condors) reported as single trades |
| **Chains** | Roll campaigns: every roll of a position as one story with total PnL |
| **Performance** | Net-liq chart, growth-of-$100, TWR/XIRR, cash-flow table |

Every view filters by account and date range. The only network call the
dashboard ever makes is the optional *Refresh marks* button (live quotes for
unrealized PnL) — everything else is served from your local database, with
charting vendored (no CDN).

## How it works (in one paragraph)

Three stages: **ingest** stores every raw broker transaction verbatim, keyed
by transaction id (idempotent, updated in place if the broker reconciles fees
overnight); **classify** turns raw rows into typed position events, flagging
anything unrecognized as `unsupported` rather than silently dropping it (see
`tastydb status`); **match** replays those events in order into lots and
closes. `tastydb process` rebuilds the derived tables from scratch every run —
raw data is the source of truth, so a rule fix or re-sync is never a
migration, just a reprocess. Lot ids are the opening broker transaction ids,
so they stay stable across rebuilds.

Known limits: stock splits, symbol changes, mergers, and ACAT transfers are
flagged rather than modeled, and realized PnL intentionally excludes
dividends/interest (those show up in the account-level returns view instead).

## Development

```sh
.venv/bin/python -m pytest tests/ -q   # fast, fully offline test suite
```

Tests never hit the network — they replay synthetic payloads shaped like real
API responses. See `CLAUDE.md` for architecture notes and invariants, and
`PLAN.md` for the roadmap.
