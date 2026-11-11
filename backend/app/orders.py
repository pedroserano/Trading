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
COMPLEX_TYPES = {
    "Put vertical": "VERTICAL",
    "Call vertical": "VERTICAL",
    "Iron condor": "IRON_CONDOR",
    "Strangle": "STRANGLE",
    "Straddle": "STRADDLE",
    "Calendar": "CALENDAR",
    "Diagonal": "DIAGONAL",
}


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
    return found[0].name if len(found) == 1 else "Custom"


def complex_type(t: Ticket) -> str:
    if len(t.legs) == 1:
        return "NONE"
    if t.intent == "roll":
        # thinkorswim labels a one-option roll by its shape: a later expiration at the same
        # strike is a CALENDAR (checked against a real thinkorswim order), at another strike a
        # DIAGONAL. Rolls of more than one option stay CUSTOM until checked the same way.
        if len(t.legs) == 2:
            a, b = t.legs
            if a.put_call == b.put_call and a.expiration != b.expiration:
                return "CALENDAR" if a.strike == b.strike else "DIAGONAL"
        return "CUSTOM"
    return COMPLEX_TYPES.get(strategy_name(t), "CUSTOM")


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
