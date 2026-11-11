"""What-if analysis of a proposed trade: payoff, breakevens, probability, greeks, margin."""

import math
from dataclasses import dataclass
from datetime import date

from .options import _ncdf, bs


@dataclass
class TradeLeg:
    put_call: str | None  # None for stock
    strike: float | None
    expiration: date | None
    quantity: float  # signed contracts (or shares for stock)
    price: float  # per share, the price used for the trade (mid by default)
    iv: float | None = None  # percent
    delta: float | None = None
    theta: float | None = None
    vega: float | None = None

    @property
    def mult(self) -> int:
        return 1 if self.put_call is None else 100


def _payoff_at_expiry(legs: list[TradeLeg], s: float) -> float:
    total = 0.0
    for leg in legs:
        if leg.put_call is None:
            value = s
        elif leg.put_call == "C":
            value = max(s - leg.strike, 0.0)
        else:
            value = max(leg.strike - s, 0.0)
        total += leg.quantity * leg.mult * (value - leg.price)
    return total


def _upside_slope(legs: list[TradeLeg]) -> float:
    """P/L change per $1 of the underlying above every strike."""
    return sum(leg.quantity * leg.mult for leg in legs if leg.put_call in ("C", None))


def _segment_prob(lo: float, hi: float, spot: float, sigma_t: float) -> float:
    """Probability the underlying ends between lo and hi (lognormal, no drift)."""

    def cdf(x):
        if x <= 0:
            return 0.0
        if math.isinf(x):
            return 1.0
        return _ncdf((math.log(x / spot) + 0.5 * sigma_t**2) / sigma_t)

    return max(0.0, cdf(hi) - cdf(lo))


def analyze(
    legs: list[TradeLeg],
    spot: float,
    today: date,
    vol: float | None = None,
    points: int = 121,
    held_shares: float = 0,
) -> dict:
    """Numbers for the trade as a whole. vol is the ATM IV in percent for probabilities."""
    if not legs or spot <= 0:
        raise ValueError("Add at least one leg")
    options = [leg for leg in legs if leg.put_call]
    dtes = [max((leg.expiration - today).days, 0) for leg in options if leg.expiration]
    first_dte = min(dtes) if dtes else 0
    t = max(first_dte, 0.5) / 365
    if vol is None:
        ivs = [leg.iv for leg in options if leg.iv]
        vol = sum(ivs) / len(ivs) if ivs else 20.0
    sigma_t = vol / 100 * math.sqrt(t)

    # Payoff is piecewise linear between strikes, so exact extremes and breakevens come
    # from evaluating at 0, each strike, and a point far above.
    strikes = sorted({leg.strike for leg in options})
    far = max([spot, *strikes]) * 3
    nodes = sorted({0.0, *strikes, far})
    values = [_payoff_at_expiry(legs, x) for x in nodes]
    slope_up = _upside_slope(legs)
    max_profit = math.inf if slope_up > 1e-9 else max(values)
    max_loss = -math.inf if slope_up < -1e-9 else min(values)

    breakevens = []
    for (x0, v0), (x1, v1) in zip(
        zip(nodes, values, strict=True), zip(nodes[1:], values[1:], strict=True), strict=False
    ):
        if v0 == 0 and x0 not in breakevens and x0 > 0:
            breakevens.append(x0)
        if (v0 < 0 < v1) or (v0 > 0 > v1):
            breakevens.append(x0 + (x1 - x0) * (-v0) / (v1 - v0))
    if values[-1] != 0 and slope_up != 0:
        # Beyond the far node the line continues; a sign change there is a breakeven.
        x_zero = nodes[-1] - values[-1] / slope_up
        if x_zero > nodes[-1]:
            breakevens.append(x_zero)
    breakevens = sorted(round(b, 2) for b in breakevens)

    # Probability of finishing above zero at the first expiration.
    edges = [0.0, *breakevens, math.inf]
    pop = 0.0
    for lo, hi in zip(edges, edges[1:], strict=False):
        mid = (lo + hi) / 2 if not math.isinf(hi) else lo * 1.5 + 1
        if _payoff_at_expiry(legs, mid) > 0:
            pop += _segment_prob(lo, hi, spot, sigma_t)

    net = sum(-leg.quantity * leg.price * leg.mult for leg in legs)  # positive = credit
    units = max((abs(leg.quantity) for leg in options), default=0)

    # Display curve: +-3 standard deviations around spot, with strikes included.
    span = max(3 * sigma_t * spot, spot * 0.03)
    lo, hi = max(spot - span, 0.01), spot + span
    xs = sorted(
        {
            *(lo + (hi - lo) * i / (points - 1) for i in range(points)),
            *(k for k in strikes if lo <= k <= hi),
        }
    )
    expiry = [round(_payoff_at_expiry(legs, x), 2) for x in xs]
    today_curve = []
    for x in xs:
        v = 0.0
        for leg in legs:
            if leg.put_call is None:
                v += leg.quantity * (x - leg.price)
                continue
            days = max((leg.expiration - today).days, 0)
            iv = (leg.iv or vol) / 100
            price = bs(leg.put_call, x, leg.strike, days / 365, iv)["price"]
            v += leg.quantity * leg.mult * (price - leg.price)
        today_curve.append(round(v, 2))

    def total(attr):
        acc = 0.0
        for leg in legs:
            if leg.put_call is None:
                acc += leg.quantity if attr == "delta" else 0.0
                continue
            g = getattr(leg, attr)
            if g is None:
                return None
            acc += leg.quantity * leg.mult * g
        return round(acc, 2)

    return {
        "net": round(net, 2),
        "credit": net > 0,
        "price_per_unit": round(abs(net) / (units * 100), 2)
        if units and not any(leg.put_call is None for leg in legs)
        else None,
        "units": units,
        "max_profit": None if math.isinf(max_profit) else round(max_profit, 2),
        "max_loss": None if math.isinf(max_loss) else round(-max_loss, 2),
        "unlimited_profit": math.isinf(max_profit),
        "unlimited_loss": math.isinf(max_loss),
        "breakevens": breakevens,
        "pop": round(pop, 4),
        "dte": first_dte,
        "vol": round(vol, 2),
        "expected_move": round(spot * sigma_t, 2),
        "delta": total("delta"),
        "theta": total("theta"),
        "vega": total("vega"),
        "bp_estimate": round(bp_estimate(legs, spot, max_loss, held_shares), 2),
        "bp_exact": not math.isinf(max_loss) and all(leg.put_call for leg in legs),
        "curve": {"x": [round(x, 2) for x in xs], "expiry": expiry, "today": today_curve},
    }


def _naked(leg: TradeLeg, spot: float, contracts: float) -> float:
    """Reg T for a short option: 20% of the price less the amount out of the money, at least
    10% of the price (calls) or strike (puts), plus the premium."""
    otm = max(leg.strike - spot, 0) if leg.put_call == "C" else max(spot - leg.strike, 0)
    floor = 0.10 * (spot if leg.put_call == "C" else leg.strike)
    return (max(0.20 * spot - otm, floor) + leg.price) * 100 * contracts


def bp_estimate(
    legs: list[TradeLeg], spot: float, max_loss: float, held_shares: float = 0
) -> float:
    """Buying power effect. Exact for defined-risk option trades (the max loss); otherwise the
    Reg T estimate: each short covered by a long of its type is a spread (its width at risk),
    each uncovered short is naked, and a short call covered by shares already held needs
    nothing more. Schwab's own figure comes from the order preview."""
    opts = [leg for leg in legs if leg.put_call]
    stock = sum(abs(leg.quantity) * spot * 0.5 for leg in legs if leg.put_call is None)
    longs_paid = sum(leg.price * 100 * leg.quantity for leg in opts if leg.quantity > 0)
    uncovered: dict[str, list[tuple[TradeLeg, float]]] = {"P": [], "C": []}
    spreads = 0.0
    for pc in ("P", "C"):
        shorts = sorted(
            (x for x in opts if x.put_call == pc and x.quantity < 0),
            key=lambda x: -_naked(x, spot, 1),
        )
        longs = [[x, x.quantity] for x in opts if x.put_call == pc and x.quantity > 0]
        cover = held_shares / 100 if pc == "C" else 0.0
        for leg in shorts:
            need = abs(leg.quantity)
            used = min(need, cover)
            need, cover = need - used, cover - used
            for lg in longs:
                if need <= 0:
                    break
                take = min(need, lg[1])
                if take <= 0:
                    continue
                lg[1] -= take
                need -= take
                width = (leg.strike - lg[0].strike) if pc == "P" else (lg[0].strike - leg.strike)
                spreads += max(width, 0) * 100 * take
            if need > 0:
                uncovered[pc].append((leg, need))
    if not uncovered["P"] and not uncovered["C"] and not math.isinf(max_loss) and not stock:
        return max(-max_loss, 0.0)
    puts = sum(_naked(x, spot, n) for x, n in uncovered["P"])
    calls = sum(_naked(x, spot, n) for x, n in uncovered["C"])
    if puts and calls:
        # Strangles: the larger side plus the premium of the other.
        other = sum(x.price * 100 * n for x, n in uncovered["C" if puts >= calls else "P"])
        req = max(puts, calls) + other
    else:
        req = puts + calls
    return req + spreads + stock + longs_paid
