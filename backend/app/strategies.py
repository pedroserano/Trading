"""Groups flat Schwab legs into strategies, and computes their numbers and rule flags.

Schwab reports positions leg by leg. Grouping is a best guess from the legs alone; a leg
held in several strategies at once can be split differently from how it was traded.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .options import NONSTANDARD, Leg, bs, day_pl


@dataclass
class Rules:
    profit_target: float = 0.50
    profit_target_straddle: float = 0.25
    # Front ratios (1x2): a share of the most they can make, the width plus the credit, which
    # is only reached at the short strike in the last days (Tony Battista: at least 10%).
    profit_target_ratio: float = 0.10
    # Calendars and diagonals: a share of the debit paid (tastylive research: 10% did best).
    profit_target_calendar: float = 0.10
    # Butterflies and broken-wing butterflies, debit or credit: a share of the most they can
    # make, the near wing's width plus the credit (tastylive: 25% to 50% of max profit, as
    # max profit needs the price at the body on expiration day).
    profit_target_fly: float = 0.25
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


def nonstandard_strategy(leg: Leg) -> Strategy:
    """A non-standard option on its own: never matched with standard legs, closing only."""
    return Strategy(NONSTANDARD, leg.underlying, [leg.with_quantity(leg.quantity)], False)


def group(legs: list[Leg]) -> list[Strategy]:
    """Groups the legs of one underlying."""
    if not legs:
        return []
    underlying = legs[0].underlying
    out: list[Strategy] = [nonstandard_strategy(x) for x in legs if x.extra.get("nonstandard")]
    legs = [x for x in legs if not x.extra.get("nonstandard")]
    pool = _Pool(legs)

    def add(name, taken, defined):
        out.append(Strategy(name, underlying, taken, defined))

    # Double calendars and double diagonals: a short put and a short call in one expiration
    # against a long put and a long call further out. Only when the front expiration holds no
    # long options, which would make its shorts part of a condor, a vertical or a lizard, and
    # the back expiration no short ones.
    for front in pool.expirations():
        if pool.options(None, False, front):
            continue
        while True:
            sp = _first(pool.options("P", True, front), key=lambda x: -x.strike)
            sc = _first(pool.options("C", True, front), key=lambda x: x.strike)
            if not (sp and sc and sp.strike <= sc.strike):
                break
            found = None
            # The back month holds the longs alone: with shorts there, its longs belong to
            # its own condors and spreads.
            for back in [
                e for e in pool.expirations() if e > front and not pool.options(None, True, e)
            ]:
                lp = _first(pool.options("P", False, back), key=lambda x: abs(x.strike - sp.strike))
                lc = _first(pool.options("C", False, back), key=lambda x: abs(x.strike - sc.strike))
                if lp and lc and lp.strike <= lc.strike:
                    found = (lp, lc)
                    break
            if not found:
                break
            lp, lc = found
            q = min(abs(x.quantity) for x in (sp, sc, lp, lc))
            taken = [pool.take(x, q) for x in (lp, sp, sc, lc)]
            same = lp.strike == sp.strike and lc.strike == sc.strike
            add("Double calendar" if same else "Double diagonal", taken, True)

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

        # Super bulls: a long call spread paid for by a short put below it; with a long put
        # below that (the modified super bull) the risk is defined.
        while True:
            sp = _first(pool.options("P", True, exp), key=lambda x: -x.strike)
            lc = sp and _first(
                [x for x in pool.options("C", False, exp) if x.strike > sp.strike],
                key=lambda x: x.strike,
            )
            sc = lc and _first(
                [x for x in pool.options("C", True, exp) if x.strike > lc.strike],
                key=lambda x: x.strike,
            )
            # A long put between them makes the short put half of a put spread instead (a
            # long iron condor).
            if not (sp and lc and sc) or any(
                sp.strike < x.strike < sc.strike for x in pool.options("P", False, exp)
            ):
                break
            lp = _first(
                [x for x in pool.options("P", False, exp) if x.strike < sp.strike],
                key=lambda x: -x.strike,
            )
            parts = [x for x in (lp, sp, lc, sc) if x]
            q = min(abs(x.quantity) for x in parts)
            add("Super bull", [pool.take(x, q) for x in parts], lp is not None)
        # Super bears: a long put spread paid for by a short call above it. With a long call
        # above that the call side is a credit spread: a delta buster (tastylive's Liz and
        # Jenny), its risk defined.
        while True:
            sc = _first(pool.options("C", True, exp), key=lambda x: x.strike)
            lp = sc and _first(
                [x for x in pool.options("P", False, exp) if x.strike < sc.strike],
                key=lambda x: -x.strike,
            )
            sp = lp and _first(
                [x for x in pool.options("P", True, exp) if x.strike < lp.strike],
                key=lambda x: -x.strike,
            )
            # A long call between them makes the short call half of a call spread instead.
            if not (sc and lp and sp) or any(
                sp.strike < x.strike < sc.strike for x in pool.options("C", False, exp)
            ):
                break
            lc = _first(
                [x for x in pool.options("C", False, exp) if x.strike > sc.strike],
                key=lambda x: x.strike,
            )
            parts = [x for x in (sp, lp, sc, lc) if x]
            q = min(abs(x.quantity) for x in parts)
            name = "Delta buster" if lc else "Super bear"
            add(name, [pool.take(x, q) for x in parts], lc is not None)

        # Big lizards: a short straddle with a long call above it; reverse big lizards: with a
        # long put below it.
        while True:
            sp = _first(pool.options("P", True, exp), key=lambda x: -x.strike)
            sc = sp and _first(
                [x for x in pool.options("C", True, exp) if x.strike == sp.strike],
                key=lambda x: x.strike,
            )
            if not (sp and sc):
                break
            lc = _first(
                [x for x in pool.options("C", False, exp) if x.strike > sc.strike],
                key=lambda x: x.strike,
            )
            lp = _first(
                [x for x in pool.options("P", False, exp) if x.strike < sp.strike],
                key=lambda x: -x.strike,
            )
            if lc and not pool.options("P", False, exp):
                q = min(abs(x.quantity) for x in (sp, sc, lc))
                add("Big lizard", [pool.take(x, q) for x in (sp, sc, lc)], False)
            elif lp and not pool.options("C", False, exp):
                q = min(abs(x.quantity) for x in (lp, sp, sc))
                add("Reverse big lizard", [pool.take(x, q) for x in (lp, sp, sc)], False)
            else:
                break

        # Reverse jade lizards (twisted sisters): a short call above a short put spread.
        while True:
            sp = _first(pool.options("P", True, exp), key=lambda x: -x.strike)
            sc = _first(pool.options("C", True, exp), key=lambda x: x.strike)
            if not (sp and sc and sp.strike < sc.strike) or pool.options("C", False, exp):
                break
            lp = _first(
                [x for x in pool.options("P", False, exp) if x.strike < sp.strike],
                key=lambda x: -x.strike,
            )
            if not lp:
                break
            q = min(abs(x.quantity) for x in (lp, sp, sc))
            add("Reverse jade lizard", [pool.take(x, q) for x in (lp, sp, sc)], False)

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

        # Butterflies, equal wings or one wider (broken wing): a long, two shorts, a long, same
        # type and expiration. Before the ratios, which would take the body and one wing.
        for pc in ("P", "C"):
            while True:
                found = None
                for s in sorted(pool.options(pc, True, exp), key=lambda x: x.strike):
                    lo = _first(
                        [x for x in pool.options(pc, False, exp) if x.strike < s.strike],
                        key=lambda x: -x.strike,
                    )
                    hi = _first(
                        [x for x in pool.options(pc, False, exp) if x.strike > s.strike],
                        key=lambda x: x.strike,
                    )
                    if lo and hi and abs(s.quantity) >= 2:
                        q = min(abs(lo.quantity), abs(hi.quantity), abs(s.quantity) // 2)
                        if q > 0:
                            found = (lo, s, hi, q)
                            break
                if not found:
                    break
                lo, s, hi, q = found
                taken = [pool.take(lo, q), pool.take(s, 2 * q), pool.take(hi, q)]
                kind = "Put" if pc == "P" else "Call"
                even = abs((s.strike - lo.strike) - (hi.strike - s.strike)) < 1e-9
                add(f"{kind} {'butterfly' if even else 'broken-wing butterfly'}", taken, True)

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

        # ZEBRAs (zero extrinsic back ratio): two longs deeper in the money than one short,
        # same type and expiration. A stock replacement: about 100 deltas per unit.
        for pc in ("P", "C"):
            while True:
                found = None
                # The short nearest the longs first: a call written further out of the money
                # against the ZEBRA is not its own short.
                shorts = sorted(pool.options(pc, True, exp), key=lambda x: x.strike)
                for s in shorts if pc == "C" else reversed(shorts):
                    deeper = [
                        lg
                        for lg in pool.options(pc, False, exp)
                        if (lg.strike > s.strike if pc == "P" else lg.strike < s.strike)
                        and abs(lg.quantity) >= 2 * abs(s.quantity)
                    ]
                    if deeper:
                        found = (s, min(deeper, key=lambda x: abs(x.strike - s.strike)))
                        break
                if not found:
                    break
                s, lg = found
                q = abs(s.quantity)
                taken = [pool.take(lg, 2 * q), pool.take(s, q)]
                kind = "Put" if pc == "P" else "Call"
                add(f"{kind} ZEBRA", sorted(taken, key=lambda x: x.strike), True)

        # Back ratios (1x2): one short, two longs further out of the money, same type and
        # expiration (a ZEBRA's longs are deeper in the money instead).
        for pc in ("P", "C"):
            while True:
                found = None
                for s in pool.options(pc, True, exp):
                    outside = [
                        lg
                        for lg in pool.options(pc, False, exp)
                        if (lg.strike < s.strike if pc == "P" else lg.strike > s.strike)
                        and abs(lg.quantity) >= 2 * abs(s.quantity)
                    ]
                    if outside:
                        found = (s, min(outside, key=lambda x: abs(x.strike - s.strike)))
                        break
                if not found:
                    break
                s, lg = found
                q = abs(s.quantity)
                taken = [pool.take(s, q), pool.take(lg, 2 * q)]
                kind = "Put" if pc == "P" else "Call"
                add(f"{kind} back ratio", sorted(taken, key=lambda x: x.strike), True)

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

    # Collars: 100 long shares per short call and long put of the same expiration.
    for st in pool.stock():
        while st.quantity >= 100:
            found = None
            for sc in sorted(pool.options("C", True), key=lambda x: x.expiration):
                lp = _first(
                    [x for x in pool.options("P", False, sc.expiration) if x.strike < sc.strike],
                    key=lambda x: -x.strike,
                )
                if lp:
                    found = (sc, lp)
                    break
            if not found:
                break
            sc, lp = found
            q = min(abs(sc.quantity), abs(lp.quantity), st.quantity // 100)
            add("Collar", [pool.take(st, q * 100), pool.take(lp, q), pool.take(sc, q)], False)

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

    cover_calls(out)
    return out


# A lone short option backed by shares or a ZEBRA: (name, stock strategy name, ZEBRA name,
# the lone short's name).
COVERED = {
    "C": ("Covered call", "Stock", "Call ZEBRA", "Short call"),
    "P": ("Covered put", "Short stock", "Put ZEBRA", "Short put"),
}


def _cover(strategies: list[Strategy], pc: str) -> tuple[dict[int, tuple], float, list[list]]:
    """Shares (long for calls, short for puts) and ZEBRAs of that type (100 shares a unit) on
    one underlying, allotted to its lone short options of that type, soonest expiration
    first: shares first, then the ZEBRA expiring soonest that still outlives the option. An
    option is covered only in full.

    Returns {id(strategy): (covered by "shares" or "ZEBRA", the expiration of the soonest ZEBRA
    used or None)} for the options covered, the shares left, and [expiration, shares left]
    for each ZEBRA.
    """
    covered_name, stock_name, zebra_name, lone_name = COVERED[pc]
    shares = float(
        sum(abs(x.quantity) for st in strategies if st.name == stock_name for x in st.legs)
    )
    zebras = sorted(
        (
            [min(x.expiration for x in st.legs), abs(_shares_like(st.name, st.legs) or 0)]
            for st in strategies
            if st.name == zebra_name
        ),
        key=lambda z: z[0],
    )
    shorts = sorted(
        (
            st
            for st in strategies
            if st.name in (lone_name, covered_name)
            and (st.name == lone_name or st.meta.get("covered_by"))
            and len(st.legs) == 1
            and st.legs[0].is_option
            and st.legs[0].put_call == pc
            and st.legs[0].short
        ),
        key=lambda st: st.legs[0].expiration,
    )
    covered: dict[int, tuple] = {}
    for st in shorts:
        leg = st.legs[0]
        need = abs(leg.quantity) * 100
        from_shares = min(shares, need)
        usable = [z for z in zebras if z[0] >= leg.expiration]
        if from_shares + sum(z[1] for z in usable) < need - 1e-9:
            continue
        shares -= from_shares
        rest = need - from_shares
        until = None
        for z in usable:
            if rest <= 1e-9:
                break
            take = min(z[1], rest)
            if take > 0 and until is None:
                until = z[0]
            z[1] -= take
            rest -= take
        covered[id(st)] = ("ZEBRA", until) if from_shares < need else ("shares", None)
    return covered, shares, zebras


def cover_calls(strategies: list[Strategy]) -> None:
    """Names a lone short call a covered call, and a lone short put a covered put, when
    shares (long for calls, short for puts) or a ZEBRA of its type on the same underlying
    back it, also when they were opened in separate orders (separate trades). A covered
    option on a ZEBRA carries the ZEBRA's expiration: it is never rolled past it.
    Recomputed from scratch, so it can run again on the same list."""
    for pc, (covered_name, _, _, lone_name) in COVERED.items():
        covered, _, _ = _cover(strategies, pc)
        for st in strategies:
            if id(st) in covered:
                st.name = covered_name
                st.meta["covered_by"], until = covered[id(st)]
                st.meta["cover_until"] = until
            elif st.name == covered_name and st.meta.get("covered_by"):
                st.name = lone_name
                st.meta.pop("covered_by")
                st.meta.pop("cover_until", None)


def free_cover(strategies: list[Strategy], expiration: date, put_call: str = "C") -> float:
    """Shares-equivalent cover still free on one underlying for a new short option of that
    type expiring then."""
    _, shares, zebras = _cover(strategies, put_call)
    return shares + sum(z[1] for z in zebras if z[0] >= expiration)


def past_cover(
    strategies: list[Strategy], closing: set[str], opening: list[tuple[str, date, float]]
) -> str | None:
    """A warning when a roll takes a covered call or put on a ZEBRA past the ZEBRA's
    expiration, after which it would be naked. closing: the symbols the roll closes;
    opening: (put_call, expiration, quantity) of the legs it opens."""
    for st in strategies:
        until = st.meta.get("cover_until")
        if st.meta.get("covered_by") != "ZEBRA" or not until:
            continue
        if not any(x.symbol in closing for x in st.legs):
            continue
        pc = st.legs[0].put_call
        if any(p == pc and q < 0 and exp > until for p, exp, q in opening):
            kind = "call" if pc == "C" else "put"
            return (
                f"Rolls the covered {kind} past its ZEBRA's expiration ({until.isoformat()}): "
                f"after that date the {kind} is no longer covered (naked). Automanage never "
                "rolls past it"
            )
    return None


RATIOS = ("Put ratio 1×2", "Call ratio 1×2")
# Managed on the short option alone; the shares (and a collar's put) stay.
COVERED_KINDS = ("Covered call", "Covered put", "Collar")
# Double calendars: tastylive's SPX study found 10% of the debit did best, as for calendars.
CALENDARS = ("Calendar", "Diagonal", "Double calendar", "Double diagonal")
# Big lizards: a short straddle with a wing, managed like a straddle (tastylive: at 25%).
BIG_LIZARDS = ("Big lizard", "Reverse big lizard")
# Recognised, with their risk, but no profit target: tastylive gives none for these (back
# ratios it does not route at all, as they are usually a debit). The DTE, tested and
# expiration rules still apply.
BEARS = ("Super bear", "Delta buster")
NO_TARGET = ("Put back ratio", "Call back ratio", "Super bull") + BEARS
FLIES = (
    "Put butterfly",
    "Call butterfly",
    "Put broken-wing butterfly",
    "Call broken-wing butterfly",
)
# Zero extrinsic back ratios (Liz and Jenny, tastylive): stock replacements, held like
# shares. Automanage leaves them alone, as it does stock, until expiration day.
ZEBRAS = ("Put ZEBRA", "Call ZEBRA")


def not_tasty(name: str, open_value: float) -> bool:
    """Debit verticals and long options: not tastylive trades, so never managed."""
    if name in ("Long put", "Long call"):
        return True
    return name in ("Put vertical", "Call vertical") and open_value < 0


def pmcc_parts(name: str, legs: list[Leg]) -> tuple[Leg, Leg] | None:
    """(long, short) of a poor man's covered call or put: a diagonal whose long, further out
    in time, is deeper in the money (a call's strike lower, a put's higher); else None."""
    if name != "Diagonal":
        return None
    longs = [x for x in legs if x.is_option and not x.short]
    shorts = [x for x in legs if x.is_option and x.short]
    if len(longs) != 1 or len(shorts) != 1:
        return None
    lg, sh = longs[0], shorts[0]
    if lg.put_call != sh.put_call:
        return None
    if lg.strike < sh.strike if lg.put_call == "C" else lg.strike > sh.strike:
        return lg, sh
    return None


def pmcc_extrinsic(lg: Leg, sh: Leg) -> float:
    """The long option's estimated time value per share when the short expires with the stock
    at the short strike (the best case), from the long's own implied volatility; 0 without it.
    Rounded down to 0.25 so small moves in IV do not keep changing the target."""
    if not lg.iv or lg.iv <= 0:
        return 0.0
    years = (lg.expiration - sh.expiration).days / 365
    # No interest rate: discounting would price a deep in-the-money put under its intrinsic
    # value, which an American option never trades at.
    value = bs(lg.put_call, sh.strike, lg.strike, years, lg.iv, r=0.0)["price"]
    return math.floor(max(value - abs(sh.strike - lg.strike), 0.0) * 4) / 4


def target_for(name: str, rules: Rules, pmcc: bool = False) -> float:
    """The profit target for a kind of trade, as a share of its basis: max profit for credit
    trades, ratios and butterflies, the debit for calendars, the call's credit for a covered
    call."""
    if name in FLIES:
        return rules.profit_target_fly
    if name in ("Straddle", "Iron fly") or name in BIG_LIZARDS:
        return rules.profit_target_straddle
    if name in RATIOS:
        return rules.profit_target_ratio
    if pmcc:
        # tastylive's strategy guide: 50% of the estimated max profit.
        return rules.profit_target
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
    """Position total in share terms. Stock counts as delta 1 and no theta, vega or gamma."""
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
    elif name in ("Long put", "Long call") or name in ZEBRAS:
        max_loss = -open_value
    elif name in CALENDARS:
        # The debit, plus, for a diagonal whose long sits further out of the money than its
        # short, that gap: a move through the short to the long at the front expiration (an
        # upper bound, as the long still has time value then).
        gap = 0.0
        for pc in ("P", "C"):
            s_ = [x.strike for x in opts if x.put_call == pc and x.short]
            l_ = [x.strike for x in opts if x.put_call == pc and not x.short]
            if s_ and l_:
                gap = max(gap, (s_[0] - l_[0]) if pc == "P" else (l_[0] - s_[0]))
        max_loss = -open_value + gap * 100 * contracts
        if pmcc := pmcc_parts(name, legs):
            # A poor man's covered call or put (tastylive's strategy guide): the width less
            # the debit, plus the long's estimated time value left when the short expires.
            lg, sh = pmcc
            q = abs(sh.quantity)
            width = abs(sh.strike - lg.strike)
            max_profit = (width + pmcc_extrinsic(lg, sh)) * 100 * q + open_value
    elif name in BIG_LIZARDS or name == "Super bull" or name in BEARS:
        # A lizard: at most the credit. A super bull: the call spread's width plus the credit;
        # its loss is defined only with the long put (the modified version). A super bear or
        # delta buster: the mirror, the put spread's width plus the credit; a delta buster's
        # long call caps the loss.
        def width(pc):
            ks = sorted(x.strike for x in opts if x.put_call == pc)
            return (ks[-1] - ks[0]) * 100 * contracts if len(ks) > 1 else 0.0

        if name in BIG_LIZARDS:
            max_profit = open_value
        elif name == "Super bull":
            max_profit = width("C") + open_value
            if st.defined_risk:
                max_loss = width("P") - open_value
        else:
            max_profit = width("P") + open_value
            if st.defined_risk:
                max_loss = width("C") - open_value
    elif name in ("Put back ratio", "Call back ratio"):
        # Worst at the longs' strike at expiration: the width less the credit (plus the
        # debit). Calls gain without limit above; puts most at zero.
        s_ = next(x for x in opts if x.short)
        l_ = next(x for x in opts if not x.short)
        q = abs(s_.quantity)
        max_loss = abs(l_.strike - s_.strike) * 100 * q - open_value
        if name == "Put back ratio":
            max_profit = (2 * l_.strike - s_.strike) * 100 * q + open_value
    elif name == "Collar":
        # The shares called away at the call's strike, or sold at the put's.
        shares = sum(x.quantity for x in legs if not x.is_option)
        call = next(x for x in opts if x.short)
        put = next(x for x in opts if not x.short)
        max_profit = shares * call.strike + open_value
        max_loss = -(shares * put.strike + open_value)
    elif name in FLIES:
        # Most at the body at expiration: the near wing's width plus the credit (less the
        # debit). A wider far wing loses its extra width beyond it (less the credit).
        body = next(x for x in opts if x.short)
        lo = min(x.strike for x in opts if not x.short)
        hi = max(x.strike for x in opts if not x.short)
        q = abs(body.quantity) / 2
        near, far = (body.strike - lo, hi - body.strike)
        if body.put_call == "P":
            near, far = far, near
        near, far = near * 100 * q, far * 100 * q
        max_profit = near + open_value
        max_loss = max(0.0, -open_value, far - near - open_value)
    elif name in RATIOS:
        # Most at the short strike at expiration: the width plus the credit.
        longs = [x for x in opts if not x.short]
        shorts_ = [x for x in opts if x.short]
        q = sum(x.quantity for x in longs)
        width = abs(longs[0].strike - shorts_[0].strike) * 100 * q if longs and shorts_ else 0
        max_profit = width + open_value
    elif open_value > 0 and opts and name not in COVERED_KINDS:
        # Jade lizards and other credit trades: at most the credit.
        max_profit = open_value

    dtes = [leg.dte(today) for leg in opts]
    shorts = [leg for leg in opts if leg.short]
    pct = pl / max_profit if (pl is not None and max_profit and max_profit > 0) else None
    upside_risk = downside_risk = None
    option_open = None
    if name in COVERED_KINDS:
        # Managed on the option alone: the shares or the ZEBRA stay.
        # A collar's target is on its call alone: the put stays as the floor.
        managed = [x for x in opts if x.short] if name == "Collar" else opts
        option_open = -sum(x.quantity * x.avg_price * x.multiplier for x in managed)
        if option_open > 0 and all(x.mark is not None for x in managed):
            opt_pl = sum(x.quantity * x.mark * x.multiplier for x in managed) + option_open
            pct = opt_pl / option_open
    elif name in CALENDARS and open_value < 0 and pl is not None and not pmcc_parts(name, legs):
        pct = pl / -open_value  # a share of the debit paid
    elif name in ("Jade lizard", "Big lizard"):
        calls = sorted(x.strike for x in opts if x.put_call == "C")
        upside_risk = (calls[-1] - calls[0]) * 100 * contracts - open_value
    elif name in ("Reverse jade lizard", "Reverse big lizard"):
        puts = sorted(x.strike for x in opts if x.put_call == "P")
        downside_risk = (puts[-1] - puts[0]) * 100 * contracts - open_value

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
        "gamma": _round(_greek(legs, "gamma"), 2),
        "short_deltas": [None if x.delta is None else round(x.delta, 3) for x in shorts],
        "short_strikes": [x.strike for x in shorts],
        # A jade lizard loses to the upside only when the credit is less than the call
        # spread's width.
        "upside_risk": None if upside_risk is None else round(max(upside_risk, 0), 2),
        # A reverse lizard loses to the downside only when the credit is less than the put
        # spread's width.
        "downside_risk": None if downside_risk is None else round(max(downside_risk, 0), 2),
        "option_open_value": None if option_open is None else round(option_open, 2),
        # A ZEBRA unit moves like 100 shares: long for calls, short for puts.
        "shares_like": _shares_like(name, opts),
        # A poor man's covered call or put: its target is a share of its estimated max profit.
        "pmcc": bool(pmcc_parts(name, legs)),
        # A covered call or put written against shares or a ZEBRA held separately, and the
        # ZEBRA's expiration: the option is never rolled past it.
        "covered_by": st.meta.get("covered_by"),
        "cover_until": (st.meta["cover_until"].isoformat() if st.meta.get("cover_until") else None),
    }

    flags = []
    target = target_for(name, rules, st.numbers["pmcc"])
    if (
        pct is not None
        and pct >= target
        and not not_tasty(name, open_value)
        and name not in NO_TARGET
    ):
        basis = (
            "of the estimated max profit"
            if st.numbers["pmcc"]
            else "of the debit"
            if name in CALENDARS
            else "of the call's credit"
            if name in ("Covered call", "Collar")
            else "of the put's credit"
            if name == "Covered put"
            else "max"
        )
        # The flag stays short; the detail says what the share is of.
        short = (
            "max"
            if st.numbers["pmcc"]
            else "debit"
            if name in CALENDARS
            else "credit"
            if name in COVERED_KINDS
            else "max"
        )
        flags.append(
            {
                "kind": "profit",
                "label": f"{round(pct * 100)}% {short}",
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
    # A ZEBRA's short sits at the money by design and is covered by the longs.
    tested = [
        x
        for x in shorts
        if name not in ZEBRAS
        and (
            (x.delta is not None and abs(x.delta) >= rules.tested_delta)
            or x.is_itm(underlying_price)
        )
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
    # A covered option's loss is offset by the shares or the ZEBRA behind it: no loss rule.
    # A super bull or bear opens for about nothing, so a multiple of its credit means little.
    if (
        name not in COVERED_KINDS
        and name not in NO_TARGET
        and not st.defined_risk
        and shorts
        and pl is not None
        and loss_basis
        and loss_basis > 0
    ):
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
    if name == NONSTANDARD:
        _nonstandard_numbers(st)


def _nonstandard_numbers(st: Strategy) -> None:
    """Its deliverable is not 100 shares of the underlying, so nothing priced as if it were
    is shown: only Schwab's market value and the P/L against what was paid. No greeks, no
    targets, no tested or DTE flags; closing only."""
    why = "; ".join(sorted({x.extra.get("nonstandard", "") for x in st.legs} - {""}))
    st.numbers.update(
        {
            "pct_max_profit": None,
            "max_profit": None,
            "max_loss": None,
            "delta": None,
            "theta": None,
            "vega": None,
            "gamma": None,
            "short_deltas": [None for _ in st.numbers.get("short_deltas", [])],
            "upside_risk": None,
            "downside_risk": None,
            "shares_like": None,
            "nonstandard": why,
        }
    )
    st.flags = [
        {
            "kind": "nonstandard",
            "label": "Close only",
            "detail": f"Non-standard option: {why}. Closing only; automanage and the scanner "
            "leave it alone. Check what it delivers in thinkorswim before closing.",
        }
    ]


def _shares_like(name: str, opts: list[Leg]) -> int | None:
    if name not in ZEBRAS:
        return None
    units = sum(abs(x.quantity) for x in opts if x.short)
    return int(units * 100 * (1 if name == "Call ZEBRA" else -1))


def _day_pl(legs: list[Leg]) -> float | None:
    parts = [day_pl(leg) for leg in legs]
    return None if any(p is None for p in parts) else round(sum(parts), 2)


def _round(v, n=2):
    return None if v is None else round(v, n)
