# tasty-db

Syncs your TastyTrade transaction history into a local SQLite database
(SQLAlchemy, so the schema ports to Postgres unchanged) and derives lot-based
realized PnL for stocks, equity options, futures, and future options.

## Confirmed API facts (developer.tastytrade.com, checked 2026-07)

- **Auth is OAuth2 only.** Session-token login is not offered to API users.
  Personal flow: create an OAuth application + personal grant at
  my.tastytrade.com → *Manage → My Profile → API → OAuth Applications*. The
  grant gives a long-lived **refresh token**; 15-minute access tokens are
  minted via `POST /oauth/token` (`grant_type=refresh_token`, `refresh_token`,
  `client_secret`).
- **Transactions:** `GET /accounts/{account_number}/transactions`, paginated
  (`page-offset`/`per-page` up to 2000, `sort=Asc`, `start-date`), unique
  integer `id` per transaction (our dedupe key). Fee fields each carry a
  `*-effect` of `Debit`/`Credit`.
- **Settlement type:** `settlement-type: "Physical" | "Cash"` on
  `EquityOption` (`GET /instruments/equity-options/{symbol}`). Real
  `FutureOption` payloads say `"Future"` instead (delivers the future) — the
  nested `future-option-product.cash-settled` boolean is the reliable flag.
  Multipliers: `shares-per-contract` (equity options), `notional-multiplier`
  (futures); for future options the documented `multiplier` field is always
  `"1.0"` in practice — the true contract multiplier is
  `notional-value / display-factor` (verified across 23 CME products).
- **Cash settlement amounts come from the broker, not from quotes.**
  Confirmed in real payloads: a cash-settled expiry posts as **two**
  `Receive Deliver` transactions per leg — `Cash Settled Exercise` /
  `Cash Settled Assignment` whose `value` is the actual settlement cash
  (its `price` field is the strike, not a close price), plus a redundant
  `Exercise`/`Assignment` removal with value 0 that the classifier skips.
  We derive the effective close price from the settlement `value` and never
  compute it from price data. Any transaction shape not recognized is flagged
  `unsupported` and shown by `tastydb status` rather than silently skipped.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

export TT_CLIENT_SECRET=...   # from your OAuth application
export TT_REFRESH_TOKEN=...   # from your personal grant
# optional: TT_CLIENT_ID, TT_ENV=sandbox, TASTYDB_DB_URL, TASTYDB_MATCH_METHOD=lifo
```

Instead of exporting, you can put the same `KEY=VALUE` lines in a `.env` file
in the working directory (or pass `--env-file path`). Real environment
variables always take precedence, and `.env` is gitignored.

Each environment gets its own database by default so sandbox testing never
touches prod data: `tastydb.sqlite3` (prod) vs `tastydb-sandbox.sqlite3`
(`--sandbox` / `TT_ENV=sandbox`). An explicit `--db` or `TASTYDB_DB_URL`
overrides both, in which case switching environments shares that one database.

## Usage

```sh
tastydb sync --backfill        # full history, all accounts (idempotent)
tastydb sync                   # incremental (re-fetches a 7-day overlap; dedupe by txn id)
tastydb process                # classify + match into lots/closes
tastydb pnl --start 2026-01-01 --end 2026-06-30
tastydb pnl --underlying SPX --group-by close_reason
tastydb status                 # ingest counts + anything needing attention
```

## Data model

- **`raw_transactions`** — one row per broker transaction (PK = TastyTrade's
  transaction id), full JSON payload plus parsed columns and a
  `processing_status`. Ingest is separate from classification, so matching
  bugs never require re-fetching.
- **`lots`** (OpenTable) — one row per opening execution. Partial closes
  decrement `remaining_quantity`; the row survives for cost-basis history
  ("open lots" = `remaining_quantity > 0`). `settlement_type` is stamped at
  open time from instrument metadata. `lot_id` IS the opening broker
  transaction id, so lot identity is stable across `process` rebuilds and
  safe for external references.
- **`lot_closes`** (CloseTable) — one row per close event per lot (a close
  spanning N lots produces N rows). Stable natural key:
  `(lot_id, broker_close_txn_id)`; the `close_id` surrogate is rebuild-scoped.
  Fees and PnL are quantized (4dp) at write time. `realized_pnl` =
  `(close_price − open_price) × quantity_closed × multiplier × side_sign − fees`
  with open/close fees allocated pro-rata. `close_reason` ∈ trade /
  expiration / assignment / exercise / cash_settlement. `linked_lot_id` points
  at the stock/futures lot opened by a physical assignment/exercise;
  `broker_close_txn_id` is NULL for synthetic worthless-expiration closes.
- **`accounts`** — cached account metadata (nickname, type), refreshed on
  every sync so `tastydb accounts` works offline.
- **`instrument_meta`** — cached multiplier/settlement metadata per symbol.
  `source` records provenance: `api` (instruments endpoints, with the
  future-option multiplier computed from notional-value/display-factor),
  `fallback` (symbology parsing + built-in contract tables; re-derived on
  every run so fixes propagate), `derived` (multiplier computed from a broker
  transaction's value), or `manual` — set `source='manual'` by hand on a row
  to pin its values against any automatic recomputation.

### Expiration paths (checked in this order)

1. **Cash-settled** (`Receive Deliver` with a cash-settled sub-type): closed at
   the broker-reported settlement cash, `reason=cash_settlement`, no linked lot.
2. **Physical assignment/exercise**: the option removal closes the lot at 0
   (premium fully realized) and the delivery leg opens/closes the stock or
   futures lot at the strike; the two are linked via `linked_lot_id`, and stock
   closed by a delivery leg inherits `reason=assignment`/`exercise`.
3. **Worthless expiration**: normally a broker `Expiration` transaction; a
   post-processing sweep also closes any lot whose expiration passed with no
   transaction (grace window of 4 days past expiry, never past the newest
   synced data), at price 0 with no broker txn id.

## Design notes & caveats

- `tastydb process` **rebuilds** `lots`/`lot_closes` from scratch on every
  run. Raw transactions are the source of truth and matching is deterministic,
  so reprocessing after a rule fix or an overnight fee reconciliation is always
  correct. Lot ids are stable anyway (they're the opening broker txn ids);
  only `close_id` is rebuild-scoped.
- Matching is per **(account, exact symbol, side)** — exact symbol rather than
  the spec's underlying+asset_type, because option symbols encode
  strike/expiration and closes must never cross contracts. FIFO within the
  group by default, LIFO via `--method lifo` / `TASTYDB_MATCH_METHOD`.
- Plain `Buy`/`Sell` actions (futures) net against the opposite side first and
  open the remainder, so crossing through zero splits correctly.
- Transactions with `reverses-id` and their targets are excluded as
  reversal pairs.
- **Not modeled:** stock splits, symbol changes, mergers, ACAT transfers
  (flagged `unsupported` in `status`), dividends/interest (ignored — this DB
  is trade PnL only), and futures daily mark-to-market cash flows (futures PnL
  is computed trade-price-to-trade-price instead, which sums to the same total
  per closed lot).
- If a close arrives with no matching open lot (backfill started
  mid-position), a warning is logged and a lot is opened in the trade's
  direction so subsequent history stays consistent.

## Tests

```sh
.venv/bin/python -m pytest tests/ -q
```
