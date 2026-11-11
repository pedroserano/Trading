"""Legs, option symbols, and Black-Scholes."""

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date

# Schwab option symbol: underlying padded to 6, YYMMDD, C/P, strike x1000 in 8 digits.
_OCC = re.compile(r"^(?P<root>.{1,6}?)\s*(?P<exp>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")

EQUITY_TYPES = {"EQUITY", "ETF", "COLLECTIVE_INVESTMENT"}


@dataclass(frozen=True)
class OptionSymbol:
    underlying: str
    expiration: date
    put_call: str  # "P" or "C"
    strike: float


def parse_option_symbol(symbol: str) -> OptionSymbol | None:
    m = _OCC.match(symbol.strip())
    if not m:
        return None
    e = m["exp"]
    return OptionSymbol(
        underlying=m["root"].strip(),
        expiration=date(2000 + int(e[:2]), int(e[2:4]), int(e[4:6])),
        put_call=m["cp"],
        strike=int(m["strike"]) / 1000,
    )


def option_symbol(underlying: str, expiration: date, put_call: str, strike: float) -> str:
    return f"{underlying:<6}{expiration:%y%m%d}{put_call}{round(strike * 1000):08d}"


@dataclass
class Leg:
    symbol: str
    underlying: str
    kind: str  # "OPTION" or "EQUITY"
    quantity: float  # signed: positive long, negative short
    avg_price: float  # per share, always positive
    put_call: str | None = None
    strike: float | None = None
    expiration: date | None = None
    multiplier: int = 1
    # Live values, per share.
    mark: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    iv: float | None = None
    extra: dict = field(default_factory=dict)

    @property
    def is_option(self) -> bool:
        return self.kind == "OPTION"

    @property
    def short(self) -> bool:
        return self.quantity < 0

    def with_quantity(self, q: float) -> "Leg":
        return replace(self, quantity=q, extra=dict(self.extra))

    def dte(self, today: date) -> int | None:
        return None if self.expiration is None else (self.expiration - today).days

    def is_itm(self, underlying_price: float | None) -> bool | None:
        if not self.is_option or underlying_price is None or self.strike is None:
            return None
        if self.put_call == "P":
            return underlying_price < self.strike
        return underlying_price > self.strike


def leg_from_schwab_position(pos: dict) -> Leg | None:
    """Builds a leg from one entry of an account's `positions` list."""
    inst = pos.get("instrument", {})
    asset = inst.get("assetType")
    qty = float(pos.get("longQuantity", 0) or 0) - float(pos.get("shortQuantity", 0) or 0)
    if qty == 0:
        return None
    if qty < 0:
        avg = pos.get("averageShortPrice") or pos.get("averagePrice")
    else:
        avg = pos.get("averageLongPrice") or pos.get("averagePrice")
    avg = abs(float(avg or 0))
    symbol = inst.get("symbol", "")
    if asset == "OPTION":
        occ = parse_option_symbol(symbol)
        if occ is None:
            return None
        put_call = {"PUT": "P", "CALL": "C"}.get(inst.get("putCall", ""), occ.put_call)
        leg = Leg(
            symbol=symbol,
            underlying=inst.get("underlyingSymbol") or occ.underlying,
            kind="OPTION",
            quantity=qty,
            avg_price=avg,
            put_call=put_call,
            strike=occ.strike,
            expiration=occ.expiration,
            multiplier=100,
        )
    elif asset in EQUITY_TYPES:
        leg = Leg(symbol=symbol, underlying=symbol, kind="EQUITY", quantity=qty, avg_price=avg)
    else:
        return None
    mv = pos.get("marketValue")
    if mv is not None:
        leg.mark = abs(float(mv)) / abs(qty * leg.multiplier)
    # What was held at the end of the previous session, for the day's P/L.
    if "previousSessionLongQuantity" in pos or "previousSessionShortQuantity" in pos:
        leg.extra["prev_qty"] = float(pos.get("previousSessionLongQuantity") or 0) - float(
            pos.get("previousSessionShortQuantity") or 0
        )
    return leg


def day_pl(leg: Leg) -> float | None:
    """Today's P/L on an open leg: what was held overnight from yesterday's close, what was
    added today from its price. None when a needed price is missing."""
    if leg.mark is None:
        return None
    prev = leg.extra.get("prev_qty", leg.quantity)
    # Only what is still held, on the same side, counts as held overnight.
    if prev * leg.quantity <= 0:
        prev = 0.0
    elif abs(prev) > abs(leg.quantity):
        prev = leg.quantity
    new = leg.quantity - prev
    close = leg.extra.get("close")
    if prev and close is None:
        return None
    pl = prev * (leg.mark - (close or 0)) + new * (leg.mark - leg.avg_price)
    return pl * leg.multiplier


# Black-Scholes, used for demo prices and as a cross-check. Volatility as a fraction.


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs(put_call: str, s: float, k: float, t_years: float, vol: float, r: float = 0.04) -> dict:
    """Price and per-share greeks. Theta per calendar day, vega per volatility point."""
    t = max(t_years, 1 / 3650)
    vol = max(vol, 1e-4)
    sq = vol * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * t) / sq
    d2 = d1 - sq
    disc = math.exp(-r * t)
    if put_call == "C":
        price = s * _ncdf(d1) - k * disc * _ncdf(d2)
        delta = _ncdf(d1)
        theta = -s * _npdf(d1) * vol / (2 * math.sqrt(t)) - r * k * disc * _ncdf(d2)
    else:
        price = k * disc * _ncdf(-d2) - s * _ncdf(-d1)
        delta = _ncdf(d1) - 1
        theta = -s * _npdf(d1) * vol / (2 * math.sqrt(t)) + r * k * disc * _ncdf(-d2)
    return {
        "price": max(price, 0.0),
        "delta": delta,
        "gamma": _npdf(d1) / (s * sq),
        "theta": theta / 365,
        "vega": s * _npdf(d1) * math.sqrt(t) / 100,
    }
