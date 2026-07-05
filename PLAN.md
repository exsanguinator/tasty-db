# tasty-db — Plan

Goal: local, queryable, lot-accurate record of all TastyTrade trading activity
(stocks, equity options, futures, future options) for realized-PnL analytics,
built on raw broker transactions so every derived number can be rebuilt and
audited.

## Status

### Phase 1 — Core pipeline ✅ (done)

- [x] Confirm API facts against official docs: OAuth2-only auth,
      transactions endpoint/pagination, `settlement-type` field on
      EquityOption/FutureOption, settlement cash surfaced in `Receive Deliver`
      transaction `value`.
- [x] SQLAlchemy models: `raw_transactions`, `lots`, `lot_closes`,
      `instrument_meta`, `sync_runs` (Postgres-portable types).
- [x] OAuth2 token manager (refresh token → 15-min access tokens).
- [x] Sync engine: backfill + incremental (7-day overlap), idempotent by
      broker txn id, payload-change detection for fee reconciliation.
- [x] Classification stage separated from ingest; unknown transaction shapes
      flagged `unsupported`, reversal pairs excluded.
- [x] FIFO/LIFO matcher: split closes across lots, partial closes, netting
      through zero for plain Buy/Sell.
- [x] Three expiration paths in priority order: cash settlement (broker
      value), physical assignment/exercise (with `linked_lot_id`), worthless
      expiration sweep (synthetic close, grace window).
- [x] Instrument metadata cache with offline fallbacks (OCC/futures symbology,
      built-in contract multiplier table).
- [x] CLI: `init-db`, `accounts`, `sync`, `process`, `pnl`, `status`.
- [x] Test suite: 27 tests, offline, covering all close reasons and both
      PnL sign conventions.

### Phase 2 — First run against real data (mostly done 2026-07-04)

- [x] Backfill real account: 12,102 transactions → 10,988 events, 6,258
      closes, 288 open lots, **zero unsupported rows**, zero warnings.
- [x] Cash settlement is a two-transaction pattern (cash txn + value-0
      removal); classifier now drops the redundant removal.
- [x] Future-option symbol parsing fixed for fixed-width heads (MES etc.).
- [x] Future-option multiplier: API `multiplier` field is bogus ("1.0");
      now computed as notional-value/display-factor, self-healed for cached
      rows, and cross-checked against broker transaction values.
- [ ] Spot-check realized PnL for a handful of known trades (one per asset
      type, one assignment, one SPX/XSP cash settlement) against the
      tastytrade app's history screen.
- [x] Sanity check: NLV-based PnL identity verified for YTD 2026 —
      `NLV_end − NLV_start − net flows` = $137,974 vs lot-realized $140,865 +
      dividends/interest $1,159; residual ≈ Δunrealized on open lots.
      External flows match hand-computed SQL to the cent. (2026-07-05)

### Phase 2.5 — Schema hardening ✅ (done 2026-07-04)

Done ahead of any external consumer (dashboard, annotations) while renames
were still free:

- [x] Stable lot identity: `lot_id` IS the opening broker txn id; close rows
      keyed by `(lot_id, broker_close_txn_id)`; `open_lots` renamed to `lots`.
- [x] Money quantization at match time (fees/PnL 4dp, prices/multipliers 8dp).
- [x] Composite indexes on `lot_closes` for the analytics query shapes.
- [x] `accounts` cache table (nickname/type), refreshed on sync, offline
      `tastydb accounts`.
- [x] Suite at 42 tests. Legacy DBs migrate automatically (derived tables
      dropped and rebuilt).
- Deferred: integer-cents money storage (only if tax-grade exactness is ever
  needed from SQLite; moot on Postgres). Alembic once the raw-table schema
  next changes — `create_all` only adds tables, never columns.

### Phase 3 — Corporate actions & completeness

- [ ] Forward/reverse splits: adjust open-lot quantity/price when a
      `Receive Deliver` split pair arrives (currently `unsupported`).
- [ ] Symbol changes / mergers: carry lots across the rename.
- [ ] ACAT transfers in: synthesize opening lots from transfer cost basis.
- [ ] Decide dividend treatment (currently ignored; could feed a separate
      income table rather than lot PnL).

### Phase 4 — Analytics depth (in progress)

- [x] Web dashboard (`tastydb dashboard`, FastAPI + Jinja, read-only):
      overview with cumulative PnL chart, closes browser with
      group-by-underlying, open positions, strategies, bookmarkable
      `/lot/{lot_id}` and `/underlying/{symbol}` pages; account + date-range
      filters on every view. (2026-07-04)
- [x] Unrealized PnL: `marks` cache table refreshed from
      `/market-data/by-type` (batched ≤100 symbols, camelCase responses);
      gross unrealized on the positions view. (2026-07-04)
- [x] Per-strategy grouping by `open_order_id` (100% coverage on Trade txns;
      `ext-group-id` rejected — only 61%). Assignment deliveries have no
      order id and group per lot. (2026-07-04)
- [x] **Order-chain grouping (rolls).** A roll order closes the old strike
      and opens the new one in a single order, so its order id appears both
      as `close_order_id` on the old lots' closes and as `open_order_id` on
      the new lots — that shared id is the chain link. `chains.assign_chains`
      walks it transitively (union-find) during `process` and stamps
      `chain_id` (root = the earliest opening order) on lots/closes.
      Dashboard: Chains view + `/chain/{id}` page with per-step activity,
      running credit, whole-campaign PnL, days in trade; strategies and lot
      pages link to their chain. Handles chains merging, partial rolls, and
      multi-underlying orders (chains keyed per account+underlying so pairs
      trades never cross-link). Verified on prod: 346 chains, biggest an
      IBIT campaign of 46 orders / 83 lots. (2026-07-04)
- [x] **Account-level returns (NLV + money flow).** `balance_snapshots`
      fetched during `tastydb sync` (EOD, back to inception where the API
      has it — 2019 for the oldest account; `/net-liq/history` fallback for
      older gaps); `cashflows.py` classifies Money Movement into external
      flows vs performance by description (sub-types are unreliable);
      `returns.py` computes daily-chained TWR, XIRR (bisection), and dollar
      PnL; `tastydb returns` CLI + dashboard `/performance` view with NLV
      and growth-of-$100 charts. Known caveat: one dormant account
      (1DA16486) has a broker-side data hole — its ~$5k funding/emptying
      transactions are absent from the API, distorting its dollar PnL and
      the combined all-time TWR at that 2020-06-20 cliff; bounded windows
      are clean. (2026-07-05)
- [ ] Wash-sale awareness for tax-oriented reports.
- [ ] Export: CSV/parquet dump of `lot_closes` for spreadsheets.
- [ ] Move to Postgres if the DB outgrows SQLite (schema already portable).

## Standing decisions

- Raw transactions are the single source of truth; `process` always rebuilds
  derived tables from scratch. Revisit only if history grows enough that a
  full replay is slow (>100k txns).
- Futures PnL is trade-price-to-trade-price per lot, not daily mark-to-market
  cash flows (same total per closed lot; MTM `Money Movement` rows stay
  ignored).
- Matching per exact symbol (not underlying) — see CLAUDE.md invariants.
- Lot identity is the opening broker txn id; external references must never
  use `close_id` (rebuild-scoped) — use `(lot_id, broker_close_txn_id)`.

## Open questions

- Whether `ext-group-id` reliably ties assignment removal legs to delivery
  legs (would replace the underlying+date linking heuristic).

## Answered (from the 2026-07-04 production backfill)

- Cash-settled sub-types are `Cash Settled Exercise` / `Cash Settled
  Assignment`, always paired with a redundant value-0 `Exercise`/`Assignment`
  removal that must be skipped.
- Expired instruments: hit-or-miss on the instruments endpoints — many
  long-expired future options still return data, some 404. Both paths covered
  (API payload → notional/display multiplier; 404 → symbology fallback +
  value-derived multiplier).
- The FutureOption API `multiplier` field is unusable (always "1.0"); use
  notional-value / display-factor.
