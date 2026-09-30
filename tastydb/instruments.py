"""Instrument metadata: multiplier + settlement type per symbol, cached in
instrument_meta.

Field sources (docs cross-checked against real production payloads, 2026-07):
- EquityOption:  settlement-type = "Physical" | "Cash", shares-per-contract
- FutureOption:  the documented `multiplier` field is USELESS — real payloads
                 carry "1.0" for every product. The true $-per-point contract
                 multiplier is notional-value / display-factor (verified across
                 23 products: ES 0.5/0.01=50, MES 0.05/0.01=5, NG 1.0/0.0001=
                 10000, ZB 1000/1.0=1000, ...). settlement-type in real
                 payloads is "Future" (delivers the future), not the
                 documented Physical/Cash; nested
                 future-option-product.cash-settled is the reliable flag.
- Future:        notional-multiplier, nested future-product.cash-settled

When the API has no record for a symbol (long-expired instruments) or we're
offline, a fallback derives everything possible from symbology plus built-in
contract-spec tables, and the row is cached with source="fallback".
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from .client import ApiError, TastyClient
from .models import AssetType, InstrumentMeta, OptionType, SettlementType
from .symbology import parse_future_symbol, parse_future_option_symbol, parse_occ_symbol

log = logging.getLogger(__name__)

INSTRUMENT_TYPE_TO_ASSET = {
    "Equity": AssetType.stock,
    "Equity Option": AssetType.equity_option,
    "Future": AssetType.future,
    "Future Option": AssetType.future_option,
}

# Roots of cash-settled index options, used only when the API can't tell us
# (settlement-type is authoritative when available).
CASH_SETTLED_OPTION_ROOTS = {
    "SPX", "SPXW", "XSP", "NDX", "NDXP", "RUT", "RUTW", "VIX", "VIXW", "DJX", "OEX", "XEO",
}

# Fallback contract multipliers by futures product code ($ per point).
FUTURES_MULTIPLIERS: dict[str, Decimal] = {
    "ES": Decimal("50"), "MES": Decimal("5"),
    "NQ": Decimal("20"), "MNQ": Decimal("2"),
    "RTY": Decimal("50"), "M2K": Decimal("5"),
    "YM": Decimal("5"), "MYM": Decimal("0.5"),
    "CL": Decimal("1000"), "MCL": Decimal("100"),
    "NG": Decimal("10000"), "QG": Decimal("2500"),
    "GC": Decimal("100"), "MGC": Decimal("10"),
    "SI": Decimal("5000"), "SIL": Decimal("1000"),
    "HG": Decimal("25000"), "MHG": Decimal("2500"),
    "ZB": Decimal("1000"), "ZN": Decimal("1000"), "ZF": Decimal("1000"), "ZT": Decimal("2000"),
    "6E": Decimal("125000"), "M6E": Decimal("12500"),
    "6B": Decimal("62500"), "M6B": Decimal("6250"),
    "6J": Decimal("12500000"), "6A": Decimal("100000"), "6C": Decimal("100000"),
    "ZC": Decimal("50"), "ZS": Decimal("50"), "ZW": Decimal("50"),
    "VX": Decimal("1000"), "VXM": Decimal("100"),
    "MBT": Decimal("0.1"), "BTC": Decimal("5"),
}

# Cash-settled futures product codes (fallback only).
CASH_SETTLED_FUTURES = {"ES", "MES", "NQ", "MNQ", "RTY", "M2K", "YM", "MYM", "VX", "VXM"}


def _settlement_from_str(value: str | None) -> SettlementType | None:
    if not value:
        return None
    v = value.strip().lower()
    if v == "cash":
        return SettlementType.cash
    if v in ("physical", "future"):  # "Future" = delivers the future contract
        return SettlementType.physical
    return None


def future_option_multiplier(payload: dict) -> Decimal | None:
    """True contract multiplier from a FutureOption API payload:
    notional-value / display-factor (the payload's own `multiplier` field is
    always 1.0 and must not be used)."""
    try:
        notional = Decimal(str(payload["notional-value"]))
        display = Decimal(str(payload["display-factor"]))
    except (KeyError, TypeError, ArithmeticError):
        return None
    if notional > 0 and display > 0:
        return notional / display
    return None


def option_fields(payload: dict) -> tuple[OptionType | None, date | None]:
    """(option_type, expiration_date) from an option instrument payload —
    authoritative where symbol parsing may not cope with a new symbol shape."""
    cp = payload.get("option-type")
    option_type = {"C": OptionType.call, "P": OptionType.put}.get(cp or "")
    try:
        expiration = date.fromisoformat(payload["expiration-date"])
    except (KeyError, TypeError, ValueError):
        expiration = None
    return option_type, expiration


class MetaProvider:
    """Resolves and caches InstrumentMeta rows. Pass client=None for offline use."""

    def __init__(self, session: Session, client: TastyClient | None = None):
        self._session = session
        self._client = client

    def get(self, symbol: str, instrument_type: str) -> InstrumentMeta:
        asset_type = INSTRUMENT_TYPE_TO_ASSET.get(instrument_type, AssetType.stock)
        meta = self._session.get(InstrumentMeta, symbol)
        if meta is not None:
            if meta.source == "fallback":
                # Fallback rows are cheap to derive, so re-derive on access:
                # symbology/contract-table fixes then propagate without a cache
                # wipe. "derived" and "manual" rows are never touched.
                fresh = self._fallback(symbol, asset_type)
                for attr in ("underlying_symbol", "multiplier", "settlement_type",
                             "strike", "option_type", "expiration_date", "contract_code"):
                    setattr(meta, attr, getattr(fresh, attr))
            elif meta.source == "api" and meta.payload and meta.asset_type in (
                AssetType.equity_option, AssetType.future_option
            ):
                # Self-heal rows cached while symbol parsing missed their shape
                # (e.g. 6-char future-option roots): the payload has the truth.
                option_type, expiration = option_fields(meta.payload)
                meta.option_type = meta.option_type or option_type
                meta.expiration_date = meta.expiration_date or expiration
            if (
                meta.source == "api"
                and meta.asset_type == AssetType.future_option
                and meta.payload
            ):
                # Self-heal rows cached before the notional/display fix: the
                # correct multiplier is recomputable from the stored payload.
                multiplier = future_option_multiplier(meta.payload)
                if multiplier is not None and multiplier != meta.multiplier:
                    log.info(
                        "corrected cached multiplier for %s: %s -> %s",
                        symbol, meta.multiplier, multiplier,
                    )
                    meta.multiplier = multiplier
            return meta
        meta = None
        if self._client is not None:
            try:
                meta = self._fetch(symbol, asset_type)
            except ApiError as exc:
                if exc.status_code == 404:
                    # expected for long-expired instruments
                    log.info("instrument %s not in API (expired?); using fallback", symbol)
                else:
                    log.warning("instrument fetch failed for %s (%s); using fallback", symbol, exc)
            except Exception as exc:  # noqa: BLE001 - degrade to fallback on any API failure
                log.warning("instrument fetch failed for %s (%s); using fallback", symbol, exc)
        if meta is None:
            meta = self._fallback(symbol, asset_type)
        self._session.add(meta)
        self._session.flush()
        return meta

    # -- API-sourced -------------------------------------------------------

    def _fetch(self, symbol: str, asset_type: AssetType) -> InstrumentMeta | None:
        assert self._client is not None
        if asset_type == AssetType.stock:
            return None  # nothing worth an API call: multiplier 1, physical
        if asset_type == AssetType.equity_option:
            d = self._client.equity_option(symbol)
            parsed = parse_occ_symbol(symbol)
            option_type, expiration = option_fields(d)
            return InstrumentMeta(
                symbol=symbol,
                asset_type=asset_type,
                underlying_symbol=d.get("underlying-symbol"),
                multiplier=Decimal(str(d.get("shares-per-contract") or 100)),
                settlement_type=_settlement_from_str(d.get("settlement-type")),
                strike=Decimal(str(d["strike-price"])) if d.get("strike-price") else (parsed.strike if parsed else None),
                option_type=option_type or (parsed.option_type if parsed else None),
                expiration_date=expiration or (parsed.expiration_date if parsed else None),
                source="api",
                payload=d,
            )
        if asset_type == AssetType.future:
            d = self._client.future(symbol)
            product = d.get("future-product") or {}
            cash = product.get("cash-settled")
            if cash is None:
                settlement = None
            else:
                settlement = SettlementType.cash if cash else SettlementType.physical
            return InstrumentMeta(
                symbol=symbol,
                asset_type=asset_type,
                underlying_symbol=symbol,
                multiplier=Decimal(str(d.get("notional-multiplier") or 1)),
                settlement_type=settlement,
                expiration_date=(
                    date.fromisoformat(d["expiration-date"])
                    if d.get("expiration-date")
                    else None
                ),
                contract_code=d.get("product-code") or parse_future_symbol(symbol),
                source="api",
                payload=d,
            )
        if asset_type == AssetType.future_option:
            d = self._client.future_option(symbol)
            parsed = parse_future_option_symbol(symbol)
            option_type, expiration = option_fields(d)
            product = d.get("future-option-product") or {}
            if product.get("cash-settled") is not None:
                settlement = (
                    SettlementType.cash if product["cash-settled"] else SettlementType.physical
                )
            else:
                settlement = _settlement_from_str(d.get("settlement-type"))
            multiplier = future_option_multiplier(d)
            if multiplier is None:
                code = d.get("product-code") or (parsed.product_code if parsed else None)
                multiplier = FUTURES_MULTIPLIERS.get(code or "", Decimal("1"))
            return InstrumentMeta(
                symbol=symbol,
                asset_type=asset_type,
                underlying_symbol=d.get("underlying-symbol") or (parsed.underlying_future if parsed else None),
                multiplier=multiplier,
                settlement_type=settlement,
                strike=Decimal(str(d["strike-price"])) if d.get("strike-price") else (parsed.strike if parsed else None),
                option_type=option_type or (parsed.option_type if parsed else None),
                expiration_date=expiration or (parsed.expiration_date if parsed else None),
                contract_code=d.get("product-code") or (parsed.product_code if parsed else None),
                source="api",
                payload=d,
            )
        return None

    # -- fallback ----------------------------------------------------------

    def _fallback(self, symbol: str, asset_type: AssetType) -> InstrumentMeta:
        if asset_type == AssetType.equity_option:
            parsed = parse_occ_symbol(symbol)
            root = parsed.root if parsed else symbol.split()[0]
            return InstrumentMeta(
                symbol=symbol,
                asset_type=asset_type,
                underlying_symbol=root,
                multiplier=Decimal("100"),
                settlement_type=(
                    SettlementType.cash
                    if root in CASH_SETTLED_OPTION_ROOTS
                    else SettlementType.physical
                ),
                strike=parsed.strike if parsed else None,
                option_type=parsed.option_type if parsed else None,
                expiration_date=parsed.expiration_date if parsed else None,
                source="fallback",
            )
        if asset_type == AssetType.future:
            code = parse_future_symbol(symbol)
            multiplier = FUTURES_MULTIPLIERS.get(code or "")
            if multiplier is None:
                log.warning("unknown futures multiplier for %s; defaulting to 1", symbol)
                multiplier = Decimal("1")
            return InstrumentMeta(
                symbol=symbol,
                asset_type=asset_type,
                underlying_symbol=symbol,
                multiplier=multiplier,
                settlement_type=(
                    SettlementType.cash
                    if code in CASH_SETTLED_FUTURES
                    else SettlementType.physical
                ),
                contract_code=code,
                source="fallback",
            )
        if asset_type == AssetType.future_option:
            parsed = parse_future_option_symbol(symbol)
            code = parsed.product_code if parsed else None
            multiplier = FUTURES_MULTIPLIERS.get(code or "")
            if multiplier is None:
                log.warning("unknown future-option multiplier for %s; defaulting to 1", symbol)
                multiplier = Decimal("1")
            return InstrumentMeta(
                symbol=symbol,
                asset_type=asset_type,
                underlying_symbol=parsed.underlying_future if parsed else None,
                multiplier=multiplier,
                settlement_type=SettlementType.physical,  # options on futures deliver the future
                strike=parsed.strike if parsed else None,
                option_type=parsed.option_type if parsed else None,
                expiration_date=parsed.expiration_date if parsed else None,
                contract_code=code,
                source="fallback",
            )
        # stock
        return InstrumentMeta(
            symbol=symbol,
            asset_type=AssetType.stock,
            underlying_symbol=symbol,
            multiplier=Decimal("1"),
            settlement_type=SettlementType.physical,
            source="fallback",
        )
