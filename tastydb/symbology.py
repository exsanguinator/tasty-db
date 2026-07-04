"""Parsers for TastyTrade symbology.

Formats (from the API Overview doc):
- Equity:        AAPL
- Equity option: OCC symbol, e.g. "AAPL  191004P00275000"
                 6-char left-justified root + yymmdd + C/P + 8-digit strike (price * 1000)
- Future:        "/ESZ9" or "/NGZ19" — slash + product code + month letter + 1-2 digit year
- Future option: "./ESZ9 EW4U9 190927P2975" — fixed-width 12-char head
                 ("./" + future symbol space-padded to 5 + option root
                 space-padded to 5), a space, then yymmdd + C/P + strike (in
                 display units). 5-char future symbols leave no space before
                 the root: "./MESM1EX3K1 210521P2920".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from .models import OptionType

FUTURE_MONTH_CODES = "FGHJKMNQUVXZ"

_FUTURE_RE = re.compile(rf"^/?([A-Z0-9]+?)([{FUTURE_MONTH_CODES}])(\d{{1,2}})$")
_OCC_RE = re.compile(r"^(\S{1,6})\s+(\d{6})([CP])(\d{8})$")
_FUT_OPT_LEG_RE = re.compile(r"^(\d{6})([CP])(\d+(?:\.\d+)?)$")


@dataclass
class ParsedOption:
    root: str
    expiration_date: date
    option_type: OptionType
    strike: Decimal


def parse_occ_symbol(symbol: str) -> ParsedOption | None:
    """Parse an OCC equity option symbol like 'AAPL  191004P00275000'."""
    m = _OCC_RE.match(symbol.strip())
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    return ParsedOption(
        root=root,
        expiration_date=datetime.strptime(ymd, "%y%m%d").date(),
        option_type=OptionType.call if cp == "C" else OptionType.put,
        strike=Decimal(strike) / 1000,
    )


def parse_future_symbol(symbol: str) -> str | None:
    """Return the product code of a futures symbol, e.g. '/ESZ9' -> 'ES'."""
    m = _FUTURE_RE.match(symbol.strip())
    return m.group(1) if m else None


@dataclass
class ParsedFutureOption:
    underlying_future: str  # e.g. "/ESZ9"
    product_code: str | None  # e.g. "ES"
    expiration_date: date
    option_type: OptionType
    strike: Decimal


def parse_future_option_symbol(symbol: str) -> ParsedFutureOption | None:
    """Parse a TW future option symbol like './ESZ9 EW4U9 190927P2975' or
    './MESM1EX3K1 210521P2920' (no space in the head when fields are full)."""
    symbol = symbol.rstrip()
    if not symbol.startswith("./") or len(symbol) < 14 or symbol[12] != " ":
        return None
    m = _FUT_OPT_LEG_RE.match(symbol[13:])
    if not m:
        return None
    ymd, cp, strike = m.groups()
    underlying = "/" + symbol[2:7].strip()
    return ParsedFutureOption(
        underlying_future=underlying,
        product_code=parse_future_symbol(underlying),
        expiration_date=datetime.strptime(ymd, "%y%m%d").date(),
        option_type=OptionType.call if cp == "C" else OptionType.put,
        strike=Decimal(strike),
    )
