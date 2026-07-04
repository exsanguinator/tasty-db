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
- [x] SQLAlchemy models: `raw_transactions`, `open_lots`, `lot_closes`,
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
- [ ] Sanity check: sum of `lot_closes.realized_pnl` + open-lot cost basis
      vs. account cash flows for a bounded date range.

### Phase 3 — Corporate actions & completeness

- [ ] Forward/reverse splits: adjust open-lot quantity/price when a
      `Receive Deliver` split pair arrives (currently `unsupported`).
- [ ] Symbol changes / mergers: carry lots across the rename.
- [ ] ACAT transfers in: synthesize opening lots from transfer cost basis.
- [ ] Decide dividend treatment (currently ignored; could feed a separate
      income table rather than lot PnL).

### Phase 4 — Analytics depth (nice to have)

- [ ] Unrealized PnL: mark open lots via `/market-data/by-type`.
- [ ] Per-strategy grouping (order-id / ext-group-id links legs of spreads).
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
