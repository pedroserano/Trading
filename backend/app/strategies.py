"""Groups flat Schwab legs into strategies, and computes their numbers and rule flags.

Schwab reports positions leg by leg. Grouping is a best guess from the legs alone; a leg
held in several strategies at once can be split differently from how it was traded.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .options import Leg, day_pl


@dataclass
class Rules:
    profit_target: float = 0.50
    profit_target_straddle: float = 0.25
    # Front ratios (1x2): a share of the most they can make, the width plus the credit, which
    # is only reached at the short strike in the last days (Tony Battista: at least 10%).
    profit_target_ratio: float = 0.10
    # Calendars and diagonals: a share of the debit paid (tastylive research: 10% did best).
    profit_target_calendar: float = 0.10
    manage_dte: int = 21
    tested_delta: float = 0.30
    loss_multiple: float = 2.0
    notify_flags: bool = True
    # Hard limits on opening trades, as shares of net liq.
    max_underlying_bp: float = 0.15
    max_index_bp: float = 0.50  # SPY and other broad index ETFs and indexes
    # Uncovered short options on one stock, at most this share of net liq in assignment
    # exposure (strike x 100 x contracts), counting its share of SPY and QQQ positions.
    max_notional: float = 0.20
    block_over_ceiling: bool = True  # refuse, not just warn, above the VIX-based ceiling
    margin_type: str = "reg_t"  # or "pm" (portfolio margin): sizing limits halve
    # Portfolio targets, as shares of net liq. Delta is beta-weighted to SPY in dollars
    # (SPY deltas times SPY's price); theta is the portfolio's daily theta.
    delta_bias: str = "neutral"  # "bullish" or "bearish" move the band (BIAS_BANDS)
    delta_min: float = -0.05
    delta_max: float = 0.05
    theta_min: float = 0.001  # tastytrade: 0.1% to 0.3% of net liq a day
    theta_max: float = 0.003


@dataclass
class Strategy:
    name: str
    underlying: str
    legs: list[Leg]
    defined_risk: bool
    # Net opened in dollars including rolls (positive = credit), when known from a journal.
    open_override: float | None = None
    meta: dict = field(default_factory=dict)
    # Filled by evaluate().
    numbers: dict = field(default_factory=dict)
    flags: list[dict] = field(default_factory=list)


class _Pool:
    """Remaining quantities while legs are matched into strategies."""

    def __init__(self, legs: list[Leg]):
        self.legs = [leg.with_quantity(leg.quantity) for leg in legs]

    def options(self, put_call=None, short=None, expiration=None):
        out = []
        for leg in self.legs:
            if not leg.is_option or leg.quantity == 0:
                continue
            if put_call and leg.put_call != put_call:
                continue
            if short is not None and leg.short != short:
                continue
            if expiration is not None and leg.expiration != expiration:
                continue
            out.append(leg)
        return out

    def stock(self):
        return [leg for leg in self.legs if leg.kind == "EQUITY" and leg.quantity != 0]

    def take(self, leg: Leg, q: float) -> Leg:
        """Removes q units (positive) from leg and returns them as a new leg."""
        sign = 1 if leg.quantity > 0 else -1
        leg.quantity -= sign * q
        return leg.with_quantity(sign * q)

    def expirations(self):
        return sorted({leg.expiration for leg in self.options()})


def group(legs: list[Leg]) -> list[Strategy]:
    """Groups the legs of one underlying."""
    if not legs:
        return []
    underlying = legs[0].underlying
    pool = _Pool(legs)
    out: list[Strategy] = []

    def add(name, taken, defined):
        out.append(Strategy(name, underlying, taken, defined))

    for exp in pool.expirations():
        # Iron condors and iron flies.
        while True:
            sp = _first(pool.options("P", True, exp), key=lambda x: -x.strike)
            sc = _first(pool.options("C", True, exp), key=lambda x: x.strike)
            if not (sp and sc and sp.strike <= sc.strike):
                break
            lp = _first(
                [x for x in pool.options("P", False, exp) if x.strike < sp.strike],
                key=lambda x: -x.strike,
            )
            lc = _first(
                [x for x in pool.options("C", False, exp) if x.strike > sc.strike],
                key=lambda x: x.strike,
            )
            if not (lp and lc):
                break
            q = min(abs(x.quantity) for x in (lp, sp, sc, lc))
            taken = [pool.take(x, q) for x in (lp, sp, sc, lc)]
            add("Iron fly" if sp.strike == sc.strike else "Iron condor", taken, True)

        # Jade lizards: a short put below a short call spread.
        while True:
            sp = _first(pool.options("P", True, exp), key=lambda x: -x.strike)
            sc = _first(pool.options("C", True, exp), key=lambda x: x.strike)
            if not (sp and sc and sp.strike < sc.strike) or pool.options("P", False, exp):
                break
            lc = _first(
                [x for x in pool.options("C", False, exp) if x.strike > sc.strike],
                key=lambda x: x.strike,
            )
            if not lc:
                break
            q = min(abs(x.quantity) for x in (sp, sc, lc))
            add("Jade lizard", [pool.take(x, q) for x in (sp, sc, lc)], False)

        # Front ratios (1x2): one long, two shorts further out of the money, same type.
        for pc in ("P", "C"):
            while True:
                found = None
                for lg in pool.options(pc, False, exp):
                    outside = [
                        s
                        for s in pool.options(pc, True, exp)
                        if (s.strike < lg.strike if pc == "P" else s.strike > lg.strike)
                        and abs(s.quantity) >= 2 * abs(lg.quantity)
                    ]
                    if outside:
                        s = min(outside, key=lambda x: abs(x.strike - lg.strike))
                        found = (lg, s)
                        break
                if not found:
                    break
                lg, s = found
                q = abs(lg.quantity)
                taken = [pool.take(lg, q), pool.take(s, 2 * q)]
                kind = "Put" if pc == "P" else "Call"
                add(f"{kind} ratio 1×2", sorted(taken, key=lambda x: x.strike), False)

        # Verticals: pair each short with the nearest long of the same type and expiration.
        for pc in ("P", "C"):
            while True:
                pairs = [
                    (abs(s.strike - lg.strike), s, lg)
                    for s in pool.options(pc, True, exp)
                    for lg in pool.options(pc, False, exp)
                ]
                if not pairs:
                    break
                _, s, lg = min(pairs, key=lambda p: p[0])
                q = min(abs(s.quantity), abs(lg.quantity))
                taken = [pool.take(lg, q), pool.take(s, q)]
                kind = "Put" if pc == "P" else "Call"
                add(f"{kind} vertical", sorted(taken, key=lambda x: x.strike), True)

        # Strangles and straddles.
        while True:
            sp = _first(pool.options("P", True, exp), key=lambda x: -x.strike)
            sc = _first(pool.options("C", True, exp), key=lambda x: x.strike)
            if not (sp and sc):
                break
            q = min(abs(sp.quantity), abs(sc.quantity))
            taken = [pool.take(sp, q), pool.take(sc, q)]
            add("Straddle" if sp.strike == sc.strike else "Strangle", taken, False)

    # Calendars and diagonals: short near-term against long further out, same type.
    for pc in ("P", "C"):
        while True:
            pairs = [
                (abs(s.strike - lg.strike), s, lg)
                for s in pool.options(pc, True)
                for lg in pool.options(pc, False)
                if lg.expiration > s.expiration
            ]
            if not pairs:
                break
            _, s, lg = min(pairs, key=lambda p: p[0])
            q = min(abs(s.quantity), abs(lg.quantity))
            taken = [pool.take(s, q), pool.take(lg, q)]
            add("Calendar" if s.strike == lg.strike else "Diagonal", taken, True)

    # Covered calls: 100 long shares per short call.
    for st in pool.stock():
        if st.quantity <= 0:
            continue
        for sc in sorted(pool.options("C", True), key=lambda x: x.expiration):
            q = min(abs(sc.quantity), st.quantity // 100)
            if q <= 0:
                break
            taken = [pool.take(st, q * 100), pool.take(sc, q)]
            add("Covered call", taken, False)

    for leg in pool.legs:
        if leg.quantity == 0:
            continue
        short = leg.short
        taken = [pool.take(leg, abs(leg.quantity))]
        if leg.is_option:
            kind = "put" if leg.put_call == "P" else "call"
            add(f"{'Short' if short else 'Long'} {kind}", taken, not short)
        else:
            add("Short stock" if short else "Stock", taken, False)

    return out


RATIOS = ("Put ratio 1×2", "Call ratio 1×2")
CALENDARS = ("Calendar", "Diagonal")


def not_tasty(name: str, open_value: float) -> bool:
    """Debit verticals and long options: not tastylive trades, so never managed."""
    if name in ("Long put", "Long call"):
        return True
    return name in ("Put vertical", "Call vertical") and open_value < 0


def target_for(name: str, rules: Rules) -> float:
    """The profit target for a kind of trade, as a share of its basis: max profit for credit
    trades and ratios, the debit for calendars, the call's credit for a covered call."""
    if name in ("Straddle", "Iron fly"):
        return rules.profit_target_straddle
    if name in RATIOS:
        return rules.profit_target_ratio
    if name in CALENDARS:
        return rules.profit_target_calendar
    return rules.profit_target


def _first(items, key):
    return min(items, key=key) if items else None


def group_all(legs: list[Leg]) -> dict[str, list[Strategy]]:
    by_under: dict[str, list[Leg]] = defaultdict(list)
    for leg in legs:
        by_under[leg.underlying].append(leg)
    return {u: group(ls) for u, ls in sorted(by_under.items())}


def _greek(legs: list[Leg], attr: str) -> float | None:
    """Position total in share terms. Stock counts as delta 1 and no theta or vega."""
    total = 0.0
    for leg in legs:
        if not leg.is_option:
            if attr == "delta":
                total += leg.quantity
            continue
        v = getattr(leg, attr)
        if v is None:
            return None
        total += leg.quantity * v * leg.multiplier
    return total


def evaluate(st: Strategy, today: date, underlying_price: float | None, rules: Rules) -> None:
    legs = st.legs
    opts = [leg for leg in legs if leg.is_option]
    # Positive = credit received.
    open_value = (
        st.open_override
        if st.open_override is not None
        else -sum(leg.quantity * leg.avg_price * leg.multiplier for leg in legs)
    )
    marks_known = all(leg.mark is not None for leg in legs)
    value = sum(leg.quantity * leg.mark * leg.multiplier for leg in legs) if marks_known else None
    pl = None if value is None else value + open_value
    contracts = max((abs(leg.quantity) for leg in opts), default=0)

    max_profit = max_loss = None
    name = st.name
    if name in ("Put vertical", "Call vertical"):
        width = abs(legs[0].strike - legs[1].strike) * 100 * contracts
        if open_value > 0:
            max_profit, max_loss = open_value, width - open_value
        else:
            max_profit, max_loss = width + open_value, -open_value
    elif name in ("Iron condor", "Iron fly"):
        puts = sorted([x.strike for x in opts if x.put_call == "P"])
        calls = sorted([x.strike for x in opts if x.put_call == "C"])
        width = max(puts[1] - puts[0], calls[1] - calls[0]) * 100 * contracts
        max_profit, max_loss = open_value, width - open_value
    elif name in ("Strangle", "Straddle", "Short put", "Short call"):
        max_profit = open_value
    elif name in ("Long put", "Long call", "Calendar", "Diagonal"):
        max_loss = -open_value
    elif name in RATIOS:
        # Most at the short strike at expiration: the width plus the credit.
        longs = [x for x in opts if not x.short]
        shorts_ = [x for x in opts if x.short]
        q = sum(x.quantity for x in longs)
        width = abs(longs[0].strike - shorts_[0].strike) * 100 * q if longs and shorts_ else 0
        max_profit = width + open_value
    elif open_value > 0 and opts and name not in ("Covered call",):
        # Jade lizards and other credit trades: at most the credit.
        max_profit = open_value

    dtes = [leg.dte(today) for leg in opts]
    shorts = [leg for leg in opts if leg.short]
    pct = pl / max_profit if (pl is not None and max_profit and max_profit > 0) else None
    upside_risk = None
    option_open = None
    if name == "Covered call":
        # Managed on the call alone: the shares stay.
        option_open = -sum(x.quantity * x.avg_price * x.multiplier for x in opts)
        if option_open > 0 and all(x.mark is not None for x in opts):
            opt_pl = sum(x.quantity * x.mark * x.multiplier for x in opts) + option_open
            pct = opt_pl / option_open
    elif name in CALENDARS and open_value < 0 and pl is not None:
        pct = pl / -open_value  # a share of the debit paid
    elif name == "Jade lizard":
        calls = sorted(x.strike for x in opts if x.put_call == "C")
        upside_risk = (calls[-1] - calls[0]) * 100 * contracts - open_value

    st.numbers = {
        "open_value": round(open_value, 2),
        "credit": open_value > 0,
        "value": None if value is None else round(value, 2),
        "pl": None if pl is None else round(pl, 2),
        "day_pl": _day_pl(legs),
        "pct_max_profit": None if pct is None else round(pct, 4),
        "max_profit": None if max_profit is None else round(max_profit, 2),
        "max_loss": None if max_loss is None else round(max_loss, 2),
        "dte": min(dtes) if dtes else None,
        "expiration": min(leg.expiration for leg in opts).isoformat() if opts else None,
        "contracts": contracts,
        "delta": _round(_greek(legs, "delta")),
        "theta": _round(_greek(legs, "theta")),
        "vega": _round(_greek(legs, "vega")),
        "short_deltas": [None if x.delta is None else round(x.delta, 3) for x in shorts],
        "short_strikes": [x.strike for x in shorts],
        # A jade lizard loses to the upside only when the credit is less than the call
        # spread's width.
        "upside_risk": None if upside_risk is None else round(max(upside_risk, 0), 2),
        "option_open_value": None if option_open is None else round(option_open, 2),
    }

    flags = []
    target = target_for(name, rules)
    if pct is not None and pct >= target and not not_tasty(name, open_value):
        basis = (
            "of the debit"
            if name in CALENDARS
            else "of the call's credit"
            if name == "Covered call"
            else "max"
        )
        flags.append(
            {
                "kind": "profit",
                "label": f"{round(pct * 100)}% {basis}",
                "detail": f"Profit target {round(target * 100)}% {basis} reached",
            }
        )
    dte = st.numbers["dte"]
    if shorts and dte is not None and dte <= rules.manage_dte:
        flags.append(
            {
                "kind": "dte",
                "label": f"{dte} DTE",
                "detail": f"At or inside {rules.manage_dte} DTE: close or roll",
            }
        )
    tested = [
        x
        for x in shorts
        if (x.delta is not None and abs(x.delta) >= rules.tested_delta)
        or x.is_itm(underlying_price)
    ]
    if tested:
        itm = any(x.is_itm(underlying_price) for x in tested)
        flags.append(
            {
                "kind": "tested",
                "label": "ITM" if itm else "Tested",
                "detail": "Short strike "
                + ", ".join(f"{x.strike:g}{x.put_call}" for x in tested)
                + (" in the money" if itm else f" at or above {rules.tested_delta:.2f} delta"),
            }
        )
    # A ratio opens for a small credit, so its loss is measured against what it can make.
    loss_basis = max_profit if name in RATIOS else open_value
    if not st.defined_risk and shorts and pl is not None and loss_basis and loss_basis > 0:
        if pl <= -rules.loss_multiple * loss_basis:
            what = "the most it can make" if name in RATIOS else "the credit received"
            flags.append(
                {
                    "kind": "loss",
                    "label": f"{rules.loss_multiple:g}x loss",
                    "detail": f"Loss at {rules.loss_multiple:g}x {what}",
                }
            )
    if any(leg.mark is None or (leg.is_option and leg.delta is None) for leg in legs):
        flags.append({"kind": "data", "label": "No quote", "detail": "Missing live quote"})
    st.flags = flags


def _day_pl(legs: list[Leg]) -> float | None:
    parts = [day_pl(leg) for leg in legs]
    return None if any(p is None for p in parts) else round(sum(parts), 2)


def _round(v, n=2):
    return None if v is None else round(v, n)
