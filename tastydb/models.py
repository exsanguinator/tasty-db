"""SQLAlchemy models.

Portability notes (for a later move to Postgres):
- All enums are stored as VARCHAR with a CHECK constraint (native_enum=False).
- Money/quantity columns are Numeric; SQLite stores them as REAL, Postgres as NUMERIC.
- Raw payloads use the generic JSON type (TEXT on SQLite, JSON/JSONB on Postgres).
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

MONEY = Numeric(20, 8)


class Base(DeclarativeBase):
    pass


def _enum(e: type[enum.Enum]) -> Enum:
    return Enum(e, native_enum=False, length=32, values_callable=lambda x: [m.value for m in x])


class AssetType(str, enum.Enum):
    stock = "stock"
    equity_option = "equity_option"
    future = "future"
    future_option = "future_option"


class Side(str, enum.Enum):
    long = "long"
    short = "short"


class SettlementType(str, enum.Enum):
    physical = "physical"
    cash = "cash"


class OptionType(str, enum.Enum):
    call = "call"
    put = "put"


class CloseReason(str, enum.Enum):
    trade = "trade"
    expiration = "expiration"
    assignment = "assignment"
    exercise = "exercise"
    cash_settlement = "cash_settlement"


class ProcessingStatus(str, enum.Enum):
    pending = "pending"  # ingested, not yet run through classify/match
    processed = "processed"  # produced position events
    ignored = "ignored"  # not position-related (money movement, dividends, ...)
    reversed = "reversed"  # reversed by a correction transaction (or is the reversal)
    unsupported = "unsupported"  # position-related but not handled (splits, symbol changes)
    error = "error"


class RawTransaction(Base):
    """One row per broker transaction, keyed by TastyTrade's transaction id.

    The full API payload is kept in `payload`; the parsed columns exist for
    indexing/inspection. Classification and matching read from this table only,
    so matching bugs never require re-fetching from the API.
    """

    __tablename__ = "raw_transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    account_number: Mapped[str] = mapped_column(String(32), index=True)
    transaction_type: Mapped[str | None] = mapped_column(String(64))
    transaction_sub_type: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str | None] = mapped_column(String(32))
    symbol: Mapped[str | None] = mapped_column(String(64), index=True)
    underlying_symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    instrument_type: Mapped[str | None] = mapped_column(String(32))
    quantity: Mapped[Decimal | None] = mapped_column(MONEY)
    price: Mapped[Decimal | None] = mapped_column(MONEY)
    value: Mapped[Decimal | None] = mapped_column(MONEY)
    value_effect: Mapped[str | None] = mapped_column(String(16))
    net_value: Mapped[Decimal | None] = mapped_column(MONEY)
    net_value_effect: Mapped[str | None] = mapped_column(String(16))
    total_fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))  # +ve = cost
    executed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    transaction_date: Mapped[date | None] = mapped_column(Date)
    payload: Mapped[dict] = mapped_column(JSON)
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        _enum(ProcessingStatus), default=ProcessingStatus.pending, index=True
    )
    processing_note: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (Index("ix_raw_txn_order", "executed_at", "id"),)


class Lot(Base):
    """One row per opening execution ("OpenTable"). Survives partial closes:
    remaining_quantity is decremented; the row stays for cost-basis history,
    so this table holds fully-closed lots too ("open" means remaining > 0).

    lot_id IS the opening broker transaction id — a deterministic, stable
    identity that survives `process` rebuilds, safe for external references
    (dashboards, annotations). broker_txn_id is kept as an explicit alias.
    """

    __tablename__ = "lots"

    lot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    broker_txn_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    underlying_symbol: Mapped[str] = mapped_column(String(32), index=True)
    asset_type: Mapped[AssetType] = mapped_column(_enum(AssetType))
    side: Mapped[Side] = mapped_column(_enum(Side))
    original_quantity: Mapped[Decimal] = mapped_column(MONEY)
    remaining_quantity: Mapped[Decimal] = mapped_column(MONEY)
    open_date: Mapped[datetime] = mapped_column(DateTime)
    open_price: Mapped[Decimal] = mapped_column(MONEY)
    open_fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    multiplier: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("1"))
    strike: Mapped[Decimal | None] = mapped_column(MONEY)
    option_type: Mapped[OptionType | None] = mapped_column(_enum(OptionType))
    expiration_date: Mapped[date | None] = mapped_column(Date, index=True)
    futures_contract_code: Mapped[str | None] = mapped_column(String(16))
    settlement_type: Mapped[SettlementType | None] = mapped_column(_enum(SettlementType))
    # broker order id of the opening trade; legs of a multi-leg order share it
    # (strategy grouping key). NULL for deliveries/sweeps (no originating order).
    open_order_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # roll-chain id: root (earliest) opening order id of the campaign this lot
    # belongs to, stamped by chains.assign_chains during `process`. NULL unless
    # the lot is linked to at least one other order by a roll.
    chain_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # structure the opening order formed ("Iron condor", "Short strangle"),
    # stamped by structures.assign_strategy_names during `process`. Every lot
    # of one opening order shares it; NULL only before a rebuild.
    strategy_name: Mapped[str | None] = mapped_column(String(64), index=True)

    closes: Mapped[list["LotClose"]] = relationship(
        back_populates="lot", foreign_keys="LotClose.lot_id"
    )


class LotClose(Base):
    """One row per close event against a lot ("CloseTable"). A single closing
    transaction that spans several lots produces several rows.

    close_id is an internal surrogate that changes on rebuild — external
    references must use the stable natural key (lot_id, broker_close_txn_id).
    account_id/symbol/underlying_symbol/asset_type/side/multiplier are
    denormalized from the lot so analytics never needs a join.
    broker_close_txn_id is NULL for synthetic closes (worthless expiration
    detected by the sweep, with no broker transaction to key off of).
    """

    __tablename__ = "lot_closes"

    close_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.lot_id"), index=True)
    broker_close_txn_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    underlying_symbol: Mapped[str] = mapped_column(String(32), index=True)
    asset_type: Mapped[AssetType] = mapped_column(_enum(AssetType))
    side: Mapped[Side] = mapped_column(_enum(Side))
    multiplier: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("1"))
    quantity_closed: Mapped[Decimal] = mapped_column(MONEY)
    open_date: Mapped[datetime] = mapped_column(DateTime)
    open_price: Mapped[Decimal] = mapped_column(MONEY)
    open_fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))  # pro-rata share
    close_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    close_price: Mapped[Decimal] = mapped_column(MONEY)
    close_fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))  # pro-rata share
    close_reason: Mapped[CloseReason] = mapped_column(_enum(CloseReason))
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    hold_days: Mapped[int] = mapped_column(Integer)
    linked_lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.lot_id"))
    open_order_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # from the lot
    close_order_id: Mapped[int | None] = mapped_column(BigInteger)  # from the closing txn
    chain_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # from the lot
    strategy_name: Mapped[str | None] = mapped_column(String(64), index=True)  # from the lot

    lot: Mapped[Lot] = relationship(back_populates="closes", foreign_keys=[lot_id])
    linked_lot: Mapped[Lot | None] = relationship(foreign_keys=[linked_lot_id])

    __table_args__ = (
        # stable natural key for external references; also the dedupe identity
        UniqueConstraint("lot_id", "broker_close_txn_id", name="uq_close_natural_key"),
        # the shapes analytics actually queries
        Index("ix_closes_account_date", "account_id", "close_date"),
        Index("ix_closes_underlying_date", "underlying_symbol", "close_date"),
    )


class Account(Base):
    """Cached account metadata from /customers/me/accounts, refreshed on every
    sync so nicknames are available offline."""

    __tablename__ = "accounts"

    account_number: Mapped[str] = mapped_column(String(32), primary_key=True)
    nickname: Mapped[str | None] = mapped_column(String(128))
    account_type_name: Mapped[str | None] = mapped_column(String(64))
    margin_or_cash: Mapped[str | None] = mapped_column(String(16))
    payload: Mapped[dict | None] = mapped_column(JSON)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class InstrumentMeta(Base):
    """Cached instrument metadata (multiplier, settlement type, contract specs).

    source == "api": from the TastyTrade instruments endpoints (trusted, kept).
    source == "fallback": derived from symbology parsing and built-in
        contract-spec tables (API had no record, e.g. long-expired instruments,
        or running offline). Re-derived on every access so parser/table fixes
        propagate automatically.
    source == "derived": multiplier computed from a broker transaction's value
        (= price * qty * multiplier for options); trusted, kept.
    source == "manual": set it to this by hand to pin a row against any
        automatic recomputation.
    """

    __tablename__ = "instrument_meta"

    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_type: Mapped[AssetType] = mapped_column(_enum(AssetType))
    underlying_symbol: Mapped[str | None] = mapped_column(String(32))
    multiplier: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("1"))
    settlement_type: Mapped[SettlementType | None] = mapped_column(_enum(SettlementType))
    strike: Mapped[Decimal | None] = mapped_column(MONEY)
    option_type: Mapped[OptionType | None] = mapped_column(_enum(OptionType))
    expiration_date: Mapped[date | None] = mapped_column(Date)
    contract_code: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16), default="fallback")
    payload: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class Mark(Base):
    """Latest cached market snapshot per open symbol (from /market-data/by-type).
    A refreshable cache, not price history — one row per symbol, overwritten."""

    __tablename__ = "marks"

    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    instrument_type: Mapped[str | None] = mapped_column(String(32))
    mark: Mapped[Decimal | None] = mapped_column(MONEY)
    bid: Mapped[Decimal | None] = mapped_column(MONEY)
    ask: Mapped[Decimal | None] = mapped_column(MONEY)
    mid: Mapped[Decimal | None] = mapped_column(MONEY)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class BalanceSnapshot(Base):
    """Daily account balance snapshot from GET /accounts/{n}/balance-snapshots.

    Fetched data (like raw_transactions), not derived — never dropped by
    `process` rebuilds. `source` records provenance: "snapshot" rows come from
    the balance-snapshots endpoint; "netliq_history" rows backfill dates older
    than the endpoint's history from /net-liq/history daily closes (which have
    no cash balance) and are never allowed to overwrite a "snapshot" row.
    """

    __tablename__ = "balance_snapshots"

    account_number: Mapped[str] = mapped_column(String(32), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)
    time_of_day: Mapped[str] = mapped_column(String(8), primary_key=True, default="EOD")
    net_liquidating_value: Mapped[Decimal] = mapped_column(MONEY)
    cash_balance: Mapped[Decimal | None] = mapped_column(MONEY)
    source: Mapped[str] = mapped_column(String(16), default="snapshot")
    payload: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class SyncRun(Base):
    """Audit log of sync runs (one row per account per invocation)."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_number: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(16))  # backfill | incremental
    start_date_used: Mapped[date | None] = mapped_column(Date)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
