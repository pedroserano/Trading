"""Order tickets: pricing and Schwab order JSON.

A ticket is a set of legs with signed quantities (positive buys, negative sells) and an
effect (opening or closing), plus a limit expressed as a signed net price per unit:
positive is a credit received, negative a debit paid. A "unit" is one of the spread, so a
2-lot vertical has legs of quantity 2 and a price per vertical, as Schwab expects.
"""

import math
from dataclasses import dataclass, field
from datetime import date
from functools import reduce

from .options import Leg, parse_option_symbol
from .strategies import group


# Our strategy names -> Schwab's complexOrderStrategyType.
class TicketError(ValueError):
    pass


@dataclass
class TicketLeg:
    symbol: str
    quantity: int  # signed: + buy, - sell
    effect: str  # "open" or "close"
    underlying: str = ""
    put_call: str = ""
    strike: float = 0.0
    expiration: date | None = None
    # Filled from quotes.
    bid: float | None = None
    ask: float | None = None
    mark: float | None = None

    @classmethod
    def parse(cls, symbol: str, quantity: int, effect: str) -> "TicketLeg":
        occ = parse_option_symbol(symbol)
        if occ is None:
            raise TicketError(f"Not an option symbol: {symbol}")
        if effect not in ("open", "close"):
            raise TicketError("Leg effect must be open or close")
        if not quantity or quantity != int(quantity):
            raise TicketError("Leg quantities must be whole, non-zero numbers")
        return cls(
            symbol=symbol,
            quantity=int(quantity),
            effect=effect,
            underlying=occ.underlying,
            put_call=occ.put_call,
            strike=occ.strike,
            expiration=occ.expiration,
        )

    @property
    def instruction(self) -> str:
        if self.effect == "open":
            return "BUY_TO_OPEN" if self.quantity > 0 else "SELL_TO_OPEN"
        return "BUY_TO_CLOSE" if self.quantity > 0 else "SELL_TO_CLOSE"


@dataclass
class Ticket:
    legs: list[TicketLeg]
    intent: str = "open"  # open, close, roll, adjust
    duration: str = "DAY"  # DAY or GTC
    limit_net: float | None = None
    trade_id: int | str | None = None
    profit_target: float | None = None  # e.g. 0.5: GTC close at 50% of max profit on fill
    warnings: list[str] = field(default_factory=list)

    @property
    def underlying(self) -> str:
        return self.legs[0].underlying

    @property
    def unit(self) -> int:
        return reduce(math.gcd, (abs(leg.quantity) for leg in self.legs))

    @property
    def units(self) -> int:
        """How many spreads: the order quantity."""
        return self.unit

    def ratios(self) -> list[int]:
        u = self.unit
        return [leg.quantity // u for leg in self.legs]


# Schwab and thinkorswim accept at most four legs in one order.
MAX_LEGS = 4


def validate_shape(t: Ticket) -> None:
    if not 1 <= len(t.legs) <= MAX_LEGS:
        raise TicketError(
            f"An order can have 1 to {MAX_LEGS} legs (Schwab's limit); this one has "
            f"{len(t.legs)}. Roll or close the put and call sides as separate orders."
        )
    if len({leg.symbol for leg in t.legs}) != len(t.legs):
        raise TicketError("The same contract appears twice")
    if len({leg.underlying for leg in t.legs}) != 1:
        raise TicketError("All legs must be on one underlying")
    if t.duration not in ("DAY", "GTC"):
        raise TicketError("Duration must be DAY or GTC")


def apply_quotes(t: Ticket, quotes: dict) -> None:
    for leg in t.legs:
        q = (quotes.get(leg.symbol) or {}).get("quote", {})
        leg.bid = _num(q.get("bidPrice", q.get("bid")))
        leg.ask = _num(q.get("askPrice", q.get("ask")))
        leg.mark = _num(q.get("mark"))
        if leg.mark is None and leg.bid is not None and leg.ask is not None:
            leg.mark = (leg.bid + leg.ask) / 2


def prices(t: Ticket) -> dict:
    """Natural, mid and far net prices per unit (positive = credit). None if quotes missing.

    Natural: sell at the bid, buy at the ask (fills now). Far: the opposite.
    """
    nat = mid = far = 0.0
    for leg, r in zip(t.legs, t.ratios(), strict=True):
        if leg.bid is None or leg.ask is None or leg.mark is None:
            return {"natural": None, "mid": None, "far": None}
        if r < 0:  # selling
            nat += -r * leg.bid
            far += -r * leg.ask
        else:
            nat -= r * leg.ask
            far -= r * leg.bid
        mid -= r * leg.mark
    return {"natural": round(nat, 2), "mid": round(mid, 2), "far": round(far, 2)}


def strategy_name(t: Ticket) -> str:
    if t.intent == "roll":
        return "Roll"
    # A closing order is named for the position it closes: buying back a strangle is a
    # strangle order, so classify with the signs flipped.
    flip = -1 if all(x.effect == "close" for x in t.legs) else 1
    legs = [
        Leg(
            symbol=x.symbol,
            underlying=x.underlying,
            kind="OPTION",
            quantity=flip * x.quantity,
            avg_price=0.0,
            put_call=x.put_call,
            strike=x.strike,
            expiration=x.expiration,
            multiplier=100,
        )
        for x in t.legs
    ]
    found = group(legs)
    if len(found) == 1:
        return found[0].name
    if (
        len(t.legs) == 2
        and _vertical(*t.legs)
        and abs(t.legs[0].quantity) != abs(t.legs[1].quantity)
    ):
        kind = "Put" if t.legs[0].put_call == "P" else "Call"
        sold = sum(-x.quantity for x in t.legs if x.quantity < 0)
        bought = sum(x.quantity for x in t.legs if x.quantity > 0)
        return f"{kind} ratio {min(sold, bought)}×{max(sold, bought)}"
    return "Custom"


def _vertical(a: TicketLeg, b: TicketLeg) -> bool:
    """Two options of one type and expiration at different strikes, one bought, one sold."""
    return (
        a.put_call == b.put_call
        and a.expiration == b.expiration
        and a.strike != b.strike
        and (a.quantity > 0) != (b.quantity > 0)
    )


def _pairs(legs: list[TicketLeg]):
    """Ways to split four legs into two pairs."""
    a, b, c, d = legs
    return [((a, b), (c, d)), ((a, c), (b, d)), ((a, d), (b, c))]


def complex_type(t: Ticket) -> str:
    """The order's strategy type as thinkorswim labels it, from the legs' shape alone.

    thinkorswim's types: single, vertical, back/ratio, calendar, diagonal, straddle,
    strangle, butterfly, condor, iron condor, vertical roll, covered stock, collar/synthetic,
    double diagonal (double calendar, straddle/strangle swap, double diagonal) and the
    unbalanced butterfly, condor, iron condor and vertical roll. Checked against real
    thinkorswim orders so far: calendar, diagonal (rolls of one option) and a broken-wing
    butterfly, which is a BUTTERFLY whatever the wing widths. Anything else is CUSTOM.
    Covered stock and collars with stock need a stock leg, which tickets never have.
    """
    legs = t.legs
    n = len(legs)
    if n == 1:
        return "NONE"
    if n == 2:
        a, b = legs
        opposite = (a.quantity > 0) != (b.quantity > 0)
        equal = abs(a.quantity) == abs(b.quantity)
        if a.put_call == b.put_call:
            if not opposite:
                return "CUSTOM"
            if a.expiration == b.expiration:
                if a.strike == b.strike:
                    return "CUSTOM"
                return "VERTICAL" if equal else "BACK_RATIO"
            if not equal:
                return "CUSTOM"
            return "CALENDAR" if a.strike == b.strike else "DIAGONAL"
        # A put and a call.
        if a.expiration != b.expiration or not equal:
            return "CUSTOM"
        if opposite:
            return "COLLAR_SYNTHETIC"
        return "STRADDLE" if a.strike == b.strike else "STRANGLE"
    if n == 3:
        lo, mid, hi = sorted(legs, key=lambda x: x.strike)
        if len({(x.put_call, x.expiration) for x in legs}) != 1:
            return "CUSTOM"
        if len({x.strike for x in legs}) != 3:
            return "CUSTOM"
        if (lo.quantity > 0) != (hi.quantity > 0) or (lo.quantity > 0) == (mid.quantity > 0):
            return "CUSTOM"
        if lo.quantity + mid.quantity + hi.quantity != 0:
            return "CUSTOM"
        return "BUTTERFLY" if lo.quantity == hi.quantity else "UNBALANCED_BUTTERFLY"
    if n != 4:
        return "CUSTOM"
    kinds = {x.put_call for x in legs}
    exps = {x.expiration for x in legs}
    if len(kinds) == 1 and len(exps) == 1 and len({x.strike for x in legs}) == 4:
        q = [x.quantity for x in sorted(legs, key=lambda x: x.strike)]
        wings_same = (q[0] > 0) == (q[3] > 0)
        body_same = (q[1] > 0) == (q[2] > 0) and (q[1] > 0) != (q[0] > 0)
        if wings_same and body_same and sum(q) == 0:
            return "CONDOR" if len({abs(v) for v in q}) == 1 else "UNBALANCED_CONDOR"
    if len(kinds) == 2 and len(exps) == 1:
        puts = sorted((x for x in legs if x.put_call == "P"), key=lambda x: x.strike)
        calls = sorted((x for x in legs if x.put_call == "C"), key=lambda x: x.strike)
        if (
            len(puts) == 2
            and _vertical(*puts)
            and _vertical(*calls)
            and abs(puts[0].quantity) == abs(puts[1].quantity)
            and abs(calls[0].quantity) == abs(calls[1].quantity)
            and puts[1].strike <= calls[0].strike
            and (puts[1].quantity > 0) == (calls[0].quantity > 0)
        ):
            balanced = abs(puts[0].quantity) == abs(calls[0].quantity)
            return "IRON_CONDOR" if balanced else "UNBALANCED_IRON_CONDOR"
    if len(kinds) == 1:
        # Two verticals: rolling one vertical to another.
        for p1, p2 in _pairs(legs):
            if (
                _vertical(*p1)
                and _vertical(*p2)
                and abs(p1[0].quantity) == abs(p1[1].quantity)
                and abs(p2[0].quantity) == abs(p2[1].quantity)
                and (
                    p1[0].expiration != p2[0].expiration
                    or {x.effect for x in p1} != {x.effect for x in p2}
                )
            ):
                same = abs(p1[0].quantity) == abs(p2[0].quantity)
                return "VERTICAL_ROLL" if same else "UNBALANCED_VERTICAL_ROLL"
    if len(kinds) == 2:
        # A put calendar or diagonal with a call calendar or diagonal: double calendar,
        # double diagonal, or a straddle/strangle rolled to another expiration.
        puts = [x for x in legs if x.put_call == "P"]
        calls = [x for x in legs if x.put_call == "C"]
        if (
            len(puts) == 2
            and all(
                (a.quantity > 0) != (b.quantity > 0) and a.expiration != b.expiration
                for a, b in (puts, calls)
            )
            and len({abs(x.quantity) for x in legs}) == 1
        ):
            return "DOUBLE_DIAGONAL"
    return "CUSTOM"


def schwab_order(t: Ticket) -> dict:
    """The order body for Schwab's place and preview endpoints."""
    if t.limit_net is None:
        raise TicketError("Set a limit price")
    price = f"{abs(t.limit_net):.2f}"
    single = len(t.legs) == 1
    if single:
        order_type = "LIMIT"
    elif t.limit_net > 0:
        order_type = "NET_CREDIT"
    elif t.limit_net < 0:
        order_type = "NET_DEBIT"
    else:
        order_type = "NET_ZERO"
    body = {
        "orderType": order_type,
        "session": "NORMAL",
        "duration": "GOOD_TILL_CANCEL" if t.duration == "GTC" else "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": complex_type(t),
        "orderLegCollection": [
            {
                "instruction": leg.instruction,
                "quantity": abs(leg.quantity),
                "instrument": {"symbol": leg.symbol, "assetType": "OPTION"},
            }
            for leg in t.legs
        ],
    }
    if order_type != "NET_ZERO":
        body["price"] = price
    if not single:
        body["quantity"] = t.units
    return body


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None
