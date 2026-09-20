# Group reports by strategy

## Context
Strategy names now exist (`structures.name_structure`, ~99% of real orders
named), but only as labels on individual rows: they are computed on the fly in
`analytics.strategies` and `chains`, so nothing can aggregate by them. The goal
is reporting — "realized PnL across all my iron condors", "credits collected
from strangles" — which means grouping by strategy wherever we group by
underlying today: the Overview and Credits pages, and the `pnl` / `credits` CLI
commands.

That requires the name to exist in the database, because Credits aggregates
`raw_transactions` and Overview aggregates `lot_closes` in SQL. Storing it is
cheap and needs no migration: `lots` / `lot_closes` are derived tables that
`rebuild_lots` wipes and rebuilds every run, and `db._ensure_derived_schema`
drops them automatically on any column mismatch.

Stamping the name once at rebuild also removes the two ad-hoc derivations
added earlier — `analytics._parse_leg_symbol` (re-parsing option symbols
because `lot_closes` lacks strike/expiry) and `chains._name_lots` — leaving one
computation point.

## Changes

### 1. Stamp `strategy_name` at rebuild time
- `models.py`: add `strategy_name: Mapped[str | None]` (indexed, VARCHAR) to
  both `Lot` (line ~157, beside `chain_id`) and `LotClose` (line ~200,
  `# from the lot`). No migration — `_ensure_derived_schema` (`db.py:25`) drops
  the derived tables on column mismatch and `process` rebuilds them.
- `structures.py`: new `assign_strategy_names(lots, closes)`, shaped like
  `chains.assign_chains`: group lots by (account, `open_order_id`), build
  `LegShape`s from the lot columns directly (side, option_type, strike,
  expiration_date, asset_type — no symbol parsing), call `name_structure`, and
  stamp every lot of the group. Lots with `open_order_id is None` (assignment
  deliveries) are named individually. Closes inherit from their lot, exactly as
  `chain_id` does.
- `matching.py`: call it in `rebuild_lots` right before `assign_chains`
  (~line 382), where `all_lots` / `all_closes` are already loaded.

### 2. Aggregate by it
- `analytics.realized_pnl`: add `"strategy": LotClose.strategy_name` to the
  `group_col` map (line ~47). Everything else (filters, ordering by summed PnL,
  `PnlRow`) already works. Coalesce NULL to "Unnamed".
- `analytics.credits_collected`: add `group_by: str = "underlying"`. The
  underlying path keeps its existing SQL aggregate. The strategy path resolves
  each raw transaction to a strategy through the derived tables and aggregates
  in Python (~13k rows, trivial):
  - opening txn → `lots.lot_id == txn.id` (lot_id IS the opening broker txn id)
  - closing txn → `lot_closes.broker_close_txn_id == txn.id`
  - neither → "Unmatched"
  **Do not** implement this as a SQL join: one close txn can close several lots,
  so joining `lot_closes` would duplicate the transaction's value and inflate
  credits. Build `dict[txn_id, name]` first, then sum each txn once.

### 3. Simplify the existing on-the-fly naming
- `analytics.strategies`: read `group[0].strategy_name` instead of naming from
  legs; delete `_parse_leg_symbol` and the `option_type` / `strike` /
  `expiration` / `asset_type` fields added to `StrategyLeg`.
- `chains`: `ChainStep.strategy_name` = the stored name off `st.opened[0]`;
  `ChainSummary.strategy_name` = the stored name off `_latest_opened(lots)[0]`.
  Delete `_name_lots` and the `replace` import.

### 4. Surfaces
- **Overview** (`web/app.py:133`, `overview.html`): `?group=strategy` switches
  the by-underlying panel to by-strategy. Small `Underlying | Strategy` toggle
  in the panel heading, preserving `filter_query`. Strategy rows are plain text
  (no link), like the existing "By close reason" panel; underlying rows keep
  their link.
- **Credits** (`web/app.py:154`, `credits.html`): the same toggle over
  `credits_collected(group_by=...)`. Charts and totals are unchanged.
- **CLI** (`cli.py:186`, `:224`): add `strategy` to `pnl --group-by`'s
  `click.Choice`; add the same `--group-by` option (`underlying|strategy`) to
  `credits`, with the column header following the choice.

## Verification
- `.venv/bin/python -m pytest tests/ -q`
- New `tests/test_strategy_grouping.py`: rebuild a fixture containing a
  vertical and a strangle, assert `lots.strategy_name` / `lot_closes` are
  stamped, that `realized_pnl(group_by="strategy")` splits the PnL correctly,
  and that `credits_collected(group_by="strategy")` attributes both the
  opening and closing txns of one trade to the same strategy (regression for
  the multi-lot double-count trap above).
- Real data: `.venv/bin/tastydb process` (required — the derived tables are
  dropped by the new columns), then `tastydb pnl --group-by strategy` and
  `tastydb credits --group-by strategy`. Cross-check that the strategy totals
  sum to the same grand total as the underlying grouping.
- `.venv/bin/tastydb dashboard` → toggle both pages between groupings.
