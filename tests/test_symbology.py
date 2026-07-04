from datetime import date
from decimal import Decimal

from tastydb.models import OptionType
from tastydb.symbology import (
    parse_future_option_symbol,
    parse_future_symbol,
    parse_occ_symbol,
)


def test_parse_occ_symbol():
    parsed = parse_occ_symbol("AAPL  191004P00275000")
    assert parsed.root == "AAPL"
    assert parsed.expiration_date == date(2019, 10, 4)
    assert parsed.option_type == OptionType.put
    assert parsed.strike == Decimal("275")


def test_parse_occ_symbol_weekly_root_and_fractional_strike():
    parsed = parse_occ_symbol("SPXW  240119C04725500")
    assert parsed.root == "SPXW"
    assert parsed.strike == Decimal("4725.5")
    assert parsed.option_type == OptionType.call


def test_parse_occ_rejects_garbage():
    assert parse_occ_symbol("AAPL") is None


def test_parse_future_symbol():
    assert parse_future_symbol("/ESZ9") == "ES"
    assert parse_future_symbol("/NGZ19") == "NG"
    assert parse_future_symbol("/M6EU5") == "M6E"
    assert parse_future_symbol("AAPL") is None


def test_parse_future_option_symbol():
    parsed = parse_future_option_symbol("./ESZ9 EW4U9 190927P2975")
    assert parsed.underlying_future == "/ESZ9"
    assert parsed.product_code == "ES"
    assert parsed.expiration_date == date(2019, 9, 27)
    assert parsed.option_type == OptionType.put
    assert parsed.strike == Decimal("2975")


def test_parse_future_option_decimal_strike():
    parsed = parse_future_option_symbol("./6EU5 EUUU5 250905C1.155")
    assert parsed.product_code == "6E"
    assert parsed.strike == Decimal("1.155")
