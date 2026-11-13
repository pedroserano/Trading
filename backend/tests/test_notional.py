"""Sizing by notional: uncovered short options on one stock, and index look-through."""

from dataclasses import dataclass
from datetime import date

import pytest

from app import guards, indexweights
from app.options import option_symbol
from app.orders import Ticket, TicketLeg, apply_quotes, prices

EXP = date(2026, 12, 18)
TODAY = date(2026, 11, 12)


def test_naked_notional_counts_only_uncovered_shorts():
    assert guards.naked_notional([("P", 160.0, -1)]) == 16_000  # the NVDA example
    assert guards.naked_notional([("P", 150.0, 1), ("P", 160.0, -1)]) == 0  # a put spread
    assert guards.naked_notional([("P", 150.0, -1), ("C", 180.0, -1)]) == 33_000
    # A 1x2 ratio: one short is covered, the other is not; the uncovered one is the worse.
    assert guards.naked_notional([("P", 160.0, 1), ("P", 150.0, -2)]) == 15_000


def nvda_put_ticket(n=1):
    sym = option_symbol("NVDA", EXP, "P", 160)
    t = Ticket(legs=[TicketLeg.parse(sym, -n, "open")], limit_net=4.0)
    apply_quotes(t, {sym: {"quote": {"bidPrice": 3.9, "askPrice": 4.1, "mark": 4.0}}})
    return t


def test_the_notional_cap_refuses_and_names_the_look_through():
    t = nvda_put_ticket(2)
    errors, _ = guards.check(
        t,
        prices(t),
        TODAY,
        limits=guards.Limits(),
        net_liq=100_000,
        notional_after=32_000,
        notional_cap=0.20,
        look_through=", including 1,200 through 7.5% of your SPY",
    )
    (e,) = [x for x in errors if "notional limit" in x]
    assert "32,000" in e and "32% of net liq, including 1,200 through 7.5% of your SPY" in e
    ok, _ = guards.check(
        nvda_put_ticket(1),
        prices(nvda_put_ticket(1)),
        TODAY,
        limits=guards.Limits(),
        net_liq=100_000,
        notional_after=16_000,
        notional_cap=0.20,
    )
    assert not [x for x in ok if "notional limit" in x]


@dataclass
class L:
    underlying: str
    put_call: str
    strike: float
    quantity: int
    expiration: date = EXP
    mark: float = 2.0
    is_option: bool = True


def test_index_positions_count_toward_their_largest_stocks():
    legs = [L("SPY", "P", 580.0, -1), L("SPY", "C", 620.0, -1), L("IWM", "P", 220.0, -1)]
    bp, notional, via = indexweights.look_through(
        "NVDA", legs, lambda s: 600.0, TODAY, guards.naked_notional
    )
    assert notional == pytest.approx(0.075 * (58_000 + 62_000))
    assert bp > 0 and via == ["7.5% of your SPY"]
    assert (
        indexweights.look_through("XOM", legs, lambda s: 600.0, TODAY, guards.naked_notional)[1]
        == 0
    )
