"""Leg-shape → friendly strategy name (tastydb/structures.py)."""
from datetime import date
from decimal import Decimal

import pytest

from tastydb.models import AssetType, OptionType, Side
from tastydb.structures import LegShape, name_structure

EXP = date(2026, 4, 17)
EXP2 = date(2026, 5, 15)


def opt(side, kind, strike, qty=1, expiration=EXP):
    return LegShape(
        side=side, option_type=kind, strike=Decimal(str(strike)),
        expiration=expiration, quantity=Decimal(str(qty)),
        asset_type=AssetType.equity_option,
    )


def share(side, qty=100):
    return LegShape(
        side=side, option_type=None, strike=None, expiration=None,
        quantity=Decimal(str(qty)), asset_type=AssetType.stock,
    )


CALL, PUT = OptionType.call, OptionType.put
LONG, SHORT = Side.long, Side.short

CASES = [
    ("Long stock", [share(LONG)]),
    ("Short stock", [share(SHORT)]),
    ("Short put", [opt(SHORT, PUT, 600)]),
    ("Long call", [opt(LONG, CALL, 600)]),
    # Verticals: the short leg nearer the money collects premium.
    ("Put credit spread", [opt(SHORT, PUT, 600), opt(LONG, PUT, 590)]),
    ("Put debit spread", [opt(LONG, PUT, 600), opt(SHORT, PUT, 590)]),
    ("Call credit spread", [opt(SHORT, CALL, 600), opt(LONG, CALL, 610)]),
    ("Call debit spread", [opt(LONG, CALL, 600), opt(SHORT, CALL, 610)]),
    # Same shape but unequal quantities is a ratio, not a vertical.
    ("Put ratio spread", [opt(SHORT, PUT, 590, qty=2), opt(LONG, PUT, 600)]),
    ("Call ratio spread", [opt(SHORT, CALL, 610, qty=3), opt(LONG, CALL, 600)]),
    ("Short strangle", [opt(SHORT, CALL, 610), opt(SHORT, PUT, 590)]),
    ("Long strangle", [opt(LONG, CALL, 610), opt(LONG, PUT, 590)]),
    ("Short straddle", [opt(SHORT, CALL, 600), opt(SHORT, PUT, 600)]),
    ("Superbull", [opt(SHORT, PUT, 590), opt(LONG, CALL, 610)]),
    ("Superbear", [opt(SHORT, CALL, 610), opt(LONG, PUT, 590)]),
    ("Calendar", [opt(SHORT, CALL, 600), opt(LONG, CALL, 600, expiration=EXP2)]),
    ("Diagonal", [opt(SHORT, CALL, 600), opt(LONG, CALL, 610, expiration=EXP2)]),
    ("Covered call", [share(LONG), opt(SHORT, CALL, 610)]),
    ("Protective put", [share(LONG), opt(LONG, PUT, 590)]),
    ("Call butterfly", [
        opt(LONG, CALL, 590), opt(SHORT, CALL, 600, qty=2), opt(LONG, CALL, 610),
    ]),
    ("Broken-wing call butterfly", [
        opt(LONG, CALL, 590), opt(SHORT, CALL, 600, qty=2), opt(LONG, CALL, 615),
    ]),
    ("Iron condor", [
        opt(LONG, PUT, 580), opt(SHORT, PUT, 590),
        opt(SHORT, CALL, 610), opt(LONG, CALL, 620),
    ]),
    ("Iron fly", [
        opt(LONG, PUT, 580), opt(SHORT, PUT, 600),
        opt(SHORT, CALL, 600), opt(LONG, CALL, 620),
    ]),
    # Unrecognized shapes fall back rather than guessing.
    ("Custom", []),
    ("Custom", [opt(SHORT, CALL, 600), opt(SHORT, CALL, 610)]),  # same side, same type
    ("Custom", [share(LONG), opt(LONG, CALL, 610)]),
    ("Custom", [opt(SHORT, PUT, 590), opt(LONG, CALL, 610, expiration=EXP2)]),
    ("Custom", [  # 3 legs, not 1-2-1
        opt(LONG, CALL, 590), opt(SHORT, CALL, 600), opt(LONG, CALL, 610),
    ]),
    ("Custom", [  # condor legs spread across two expirations
        opt(LONG, PUT, 580), opt(SHORT, PUT, 590),
        opt(SHORT, CALL, 610), opt(LONG, CALL, 620, expiration=EXP2),
    ]),
    ("Custom", [opt(SHORT, PUT, 590)] * 5),
]


@pytest.mark.parametrize("expected,legs", CASES, ids=[
    f"{i}-{name}" for i, (name, _) in enumerate(CASES)
])
def test_name_structure(expected, legs):
    assert name_structure(legs) == expected


def test_leg_order_does_not_matter():
    legs = [
        opt(LONG, PUT, 580), opt(SHORT, PUT, 590),
        opt(SHORT, CALL, 610), opt(LONG, CALL, 620),
    ]
    assert name_structure(list(reversed(legs))) == "Iron condor"


def test_future_leg_named_future():
    leg = LegShape(
        side=Side.long, option_type=None, strike=None, expiration=None,
        quantity=Decimal("1"), asset_type=AssetType.future,
    )
    assert name_structure([leg]) == "Long future"
