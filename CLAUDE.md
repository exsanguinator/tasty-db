# tasty-db

Syncs TastyTrade transaction history into SQLite (SQLAlchemy 2.0) and derives
lot-based realized PnL for stocks, equity options, futures, and future options.
See README.md for user-facing docs and PLAN.md for the roadmap.

## Commands

```sh
.venv/bin/python -m pytest tests/ -q      # run tests (fast, no network)
.venv/bin/pip install -e ".[dev]"         # (re)install after dependency changes
.venv/bin/tastydb --help                  # CLI entry point
```

The venv is `.venv/` (Python 3.14). Tests never hit the network — they seed
raw payloads via `tests/conftest.py:make_txn` and run the pipeline offline.

## Architecture: a three-stage pipeline

1. **Ingest** (`ingest.py`): API → `raw_transactions`. Keyed by TastyTrade's
   transaction id, full JSON payload stored, idempotent. If a payload changed
   on re-sync (overnight fee reconciliation), the row is updated in place.
2. **Classify** (`classify.py`): raw rows → typed `PositionEvent`s
   (OPEN / CLOSE / NET), pure function of raw data. Every raw row gets a
   `processing_status`; unknown position-affecting shapes become
   `unsupported`, never silently dropped.
3. **Match** (`matching.py`): replays events in (executed_at, id) order into
   `lots` / `lot_closes`, FIFO or LIFO. **`rebuild_lots` wipes and rebuilds
   the derived tables every run** — raw is the source of truth, so never
   write incremental updates to lots/closes. Identity is still stable:
   `lot_id` IS the opening broker txn id, and a close row's natural key is
   (lot_id, broker_close_txn_id); only `close_id` is a rebuild-scoped
   surrogate.

Supporting modules: `auth.py` (OAuth2 refresh-token → 15-min access tokens),
`client.py` (REST, pagination, market data), `instruments.py`
(multiplier/settlement-type cache with offline symbology fallbacks),
`symbology.py` (OCC/futures symbol parsers), `analytics.py` (PnL aggregation,
positions, strategies), `chains.py` (roll-chain grouping + campaign
analytics), `marks.py` (mark cache for unrealized PnL), `cashflows.py`
(Money Movement → external-flow vs performance classification, computed on
demand from raw), `returns.py` (account TWR/XIRR from `balance_snapshots` +
external flows), `cli.py` (click), `web/` (FastAPI + Jinja dashboard, served
by `tastydb dashboard`; read-only except POST /marks/refresh).
`forward.py` (repo root, not part of the package, stdlib only) is a TCP relay
that exposes the loopback-bound dashboard on the LAN (0.0.0.0:8787 →
127.0.0.1:8787); keep its ports in sync with the `dashboard` CLI defaults.

Account returns: `tastydb sync` also upserts EOD `balance_snapshots`
(per-account PK (account, date, time_of_day); older-than-history gaps
backfilled from `/net-liq/history` with `source='netliq_history'`, which
never overwrites a real `source='snapshot'` row). `returns.py` chains daily
TWR links `r=(NLV−F)/NLV_prev` (EOD flow convention; zero/negative-base
links skipped, never sign-flipped) and solves XIRR by bisection (needs flows
in both directions, else None). Money Movement sub-types LIE: interest,
dividends, and rebates appear under "Deposit", margin interest under
"Withdrawal" — `cashflows.py` classifies by symbol-presence (dividends/MTM
carry one, true flows never do) then description patterns; unrecognized
flow-claiming rows become `unclassified_flow` (treated external, listed by
`tastydb status`). Journals between own accounts cancel in combined views.

Strategy grouping keys on `open_order_id` (the opening trade's broker
order-id, present on 100% of Trade txns; NULL on Receive Deliver, so
assignment deliveries group per lot). Roll chains: a roll order's id appears
as both `close_order_id` on the old lots' closes and `open_order_id` on the
new lots; `chains.assign_chains` (end of `rebuild_lots`) union-finds those
links per (account, underlying) and stamps `chain_id` = the root (earliest)
opening order id. Only real chains (≥2 linked orders) get one; order-less
closes (sweeps/settlements) inherit it through their lot. Derived-table
schema changes need no migration: `db._ensure_derived_schema` drops
`lots`/`lot_closes` on any column mismatch and `process` rebuilds them.

## Invariants — do not break

- **Matching key is (account, exact symbol, side)**, not underlying: option
  symbols encode strike/expiry; matching on underlying would cross-close
  different contracts.
- **Cash settlement is checked before expiration** in `classify.py`: an ITM
  cash-settled option at expiry has real PnL (the broker txn's `value` is the
  official settlement cash) and must not book as a worthless expiration.
- **Close prices for cash settlements come from the broker's `value`**, never
  from quotes: `close_price = |value| / (qty × multiplier)`.
- Partial closes decrement `remaining_quantity`; fully closed lots keep their
  row (remaining 0). "Open lots" means `remaining_quantity > 0`.
- The expiration sweep only closes lots ≥4 days (grace) past expiry and never
  past the newest synced transaction — otherwise a not-yet-synced settlement
  transaction would be misbooked as worthless expiry.
- Money columns are `Numeric`, enums are `native_enum=False` VARCHAR, payloads
  are generic `JSON` — keep it that way so the schema ports to Postgres.
- Fees and realized PnL are quantized to 4dp, prices/multipliers to 8dp, at
  match time (`matching.Q_MONEY`/`Q_PRICE`) — never write unquantized
  divisions to money columns (SQLite stores them as floats).
- Timestamps are stored UTC-naive (converted on ingest).
- Day bucketing (PnL/credits charts) and `--start`/`--end`/dashboard date
  filters use the **UTC date** of `executed_at`/`close_date`. The broker's
  `transaction_date` differs for ~0.5% of real rows: futures options traded
  ~6–7pm ET (CME session → next trade date) or on exchange holidays (next
  business day) and evening-ET crypto (broker date = previous day); equities
  and equity options always match. Cash flows/returns use `transaction_date`.
  Accepted as-is. Converting to ET would not fix it; the real fix is
  bucketing by `transaction_date` (needs a trade-date column on `lot_closes`).
- Fee sign convention: positive = cost (`Debit`), negative = rebate.

## API facts (verified 2026-07; docs mirror in CLAUDE.local.md reference)

- OAuth2 only; `POST /oauth/token` with grant_type=refresh_token +
  client_secret. Requests need a `User-Agent` header or get rejected.
- `GET /accounts/{n}/transactions`: per-page max 2000, `sort=Asc`,
  envelope `{"data": {"items": [...]}, "pagination": {...}}`.
- `settlement-type: "Physical"|"Cash"` on EquityOption instruments;
  multipliers: `shares-per-contract` (equity options), `notional-multiplier`
  (futures).
- **Docs lie about FutureOption fields** (verified against real payloads
  2026-07): the `multiplier` field is always `"1.0"` — the true $-per-point
  multiplier is `notional-value / display-factor` (ES 0.5/0.01=50,
  MES 0.05/0.01=5; verified across 23 products). And `settlement-type` is
  `"Future"` (delivers the future), not Physical/Cash — use nested
  `future-option-product.cash-settled` instead. See
  `instruments.future_option_multiplier`.
- **Cash settlement is TWO transactions per leg** (real payloads): a
  `Cash Settled Exercise/Assignment` carrying the settlement cash in `value`
  (its `price` field is the strike — never use it as a close price), then a
  plain `Exercise`/`Assignment` removal with value 0. The classifier drops
  the removal when a cash-settled sibling exists for the same symbol+date
  (`classify_all`), otherwise it would double-close.
- Future-option symbols have a fixed-width 12-char head (`./` + future
  padded to 5 + option root padded to 5); 5-char futures like `MESM1` leave
  no space between the fields — never split on whitespace.
- Option trade `value` = price × qty × multiplier exactly (broker-computed),
  so the matcher derives multipliers from it when metadata is guessed
  (`Matcher._resolve_multiplier`). NOT true for outright futures, where
  `value` is settlement cash.
- `transaction-sub-type` values are NOT exhaustively documented; the
  classifier matches case-insensitively ("cash settled", "assignment", ...)
  and flags the rest. If real data surfaces a new sub-type, add a rule in
  `classify.py` and a regression test in `tests/test_prod_patterns.py` —
  `tastydb status` lists unhandled rows.

## Conventions

- Credentials via env (`TT_CLIENT_SECRET`, `TT_REFRESH_TOKEN`), optionally
  seeded from a gitignored `.env` file (`config.load_dotenv`; real env vars
  win). Never stored in the DB or repo.
- Each environment gets its own default database (`tastydb.sqlite3` prod,
  `tastydb-sandbox.sqlite3` sandbox) via `Config.resolved_db_url`; an
  explicit `--db`/`TASTYDB_DB_URL` overrides both.
- `instrument_meta.source` semantics: `api` and `derived` are trusted,
  `fallback` is re-derived on every access, `manual` is a user-pinned row
  that must never be recomputed (see models.py docstring).
- Every classify/match behavior gets a test in `tests/` using synthetic
  payloads shaped like real API responses (dasherized keys).
- Degrade gracefully offline: `MetaProvider(client=None)` falls back to
  symbology parsing + built-in contract-spec tables (`FUTURES_MULTIPLIERS`).
