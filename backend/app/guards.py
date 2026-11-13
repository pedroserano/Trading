"""Checks every order must pass before it is sent anywhere. Pure functions.

Errors block the order. Warnings are shown on the ticket and need no override.
"""

from dataclasses import dataclass
from datetime import date

from .orders import Ticket

# Broad index ETFs and indexes: they get their own, higher per-underlying limit.
INDEX_UNDERLYINGS = {
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "SPX",
    "XSP",
    "NDX",
    "RUT",
    "$SPX",
    "$XSP",
    "$NDX",
    "$RUT",
}


def naked_notional(legs) -> float:
    """Assignment exposure of uncovered short options, per side: for each of puts and calls,
    the shorts not matched by longs, at the shorts' strikes, times 100. Longs cover the
    furthest-out shorts first, so what is left uncovered is the worst case.
    legs: (put_call, strike, signed quantity) for option legs."""
    total = 0.0
    for pc in ("P", "C"):
        side = [(k, q) for p, k, q in legs if p == pc and k is not None]
        longs = sum(q for _, q in side if q > 0)
        shorts = sorted(
            ((k, -q) for k, q in side if q < 0), key=lambda x: x[0] if pc == "P" else -x[0]
        )
        for k, n in shorts:
            covered = min(longs, n)
            longs -= covered
            total += (n - covered) * k * 100
    return total


@dataclass
class Limits:
    max_contracts: int = 10
    # Defined-risk and undefined-risk size guides, as a share of net liq.
    size_warn_defined: float = 0.03
    size_warn_undefined: float = 0.05
    # Hard stop on any one order, as a share of net liq.
    size_block: float = 0.25


def check(
    t: Ticket,
    p: dict,
    today: date,
    *,
    limits: Limits,
    held: dict[str, int] | None = None,
    net_liq: float | None = None,
    bp_effect: float | None = None,
    defined_risk: bool = True,
    bp_used_after: float | None = None,
    bp_ceiling: float | None = None,
    block_over_ceiling: bool = False,
    underlying_bp_after: float | None = None,
    underlying_cap: float | None = None,
    notional_after: float | None = None,
    notional_cap: float | None = None,
    look_through: str = "",
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for leg in t.legs:
        if not leg.equity and abs(leg.quantity) > limits.max_contracts:
            errors.append(
                f"{abs(leg.quantity)} contracts is above the "
                f"{limits.max_contracts}-contract limit per leg"
            )
            break
    for leg in t.legs:
        if leg.expiration and leg.expiration < today:
            errors.append(f"{leg.symbol.strip()} has expired")
        if leg.bid is None or leg.ask is None:
            errors.append(f"No live quote for {leg.symbol.strip()}")

    if t.limit_net is None:
        errors.append("Set a limit price")
    elif p.get("natural") is not None and p.get("far") is not None:
        lo, hi = p["natural"], p["far"]
        tol = max(0.05, 0.1 * (hi - lo))
        if t.limit_net < lo - tol:
            errors.append(
                f"Limit {_fmt(t.limit_net)} is worse than the natural price "
                f"{_fmt(lo)}; check for a typo"
            )
        elif t.limit_net > hi + tol:
            errors.append(
                f"Limit {_fmt(t.limit_net)} is beyond the far side {_fmt(hi)} and would not fill"
            )

    # Closing legs must close something that is held, in the right direction and size.
    if held is not None:
        for leg in t.legs:
            if leg.effect != "close":
                continue
            have = held.get(leg.symbol, 0)
            if have == 0 or (have > 0) == (leg.quantity > 0) or abs(leg.quantity) > abs(have):
                errors.append(
                    f"{leg.symbol.strip()}: closes {abs(leg.quantity)} but the "
                    f"position holds {have}"
                )

    if net_liq and bp_effect is not None and t.intent in ("open", "adjust"):
        share = bp_effect / net_liq
        if share > limits.size_block:
            errors.append(
                f"Buying power effect is {share:.0%} of net liq, above the "
                f"{limits.size_block:.0%} hard limit"
            )
        else:
            guide = limits.size_warn_defined if defined_risk else limits.size_warn_undefined
            if share > guide:
                warnings.append(
                    f"Buying power effect is {share:.1%} of net liq; the guide is "
                    f"{guide:.0%} for {'defined' if defined_risk else 'undefined'} "
                    "risk"
                )
    opening = t.intent in ("open", "adjust")
    if bp_used_after is not None and bp_ceiling is not None and bp_used_after > bp_ceiling:
        text = (
            f"Buying power used would be {bp_used_after:.0%}, above the "
            f"{bp_ceiling:.0%} ceiling for the current VIX"
        )
        (errors if block_over_ceiling and opening else warnings).append(text)
    if (
        opening
        and net_liq
        and underlying_bp_after is not None
        and underlying_cap is not None
        and underlying_bp_after > underlying_cap * net_liq
    ):
        errors.append(
            f"{t.underlying} would use {underlying_bp_after / net_liq:.0%} of net liq in buying "
            f"power, above the {underlying_cap:.0%} limit for one underlying"
        )
    if (
        opening
        and net_liq
        and notional_after is not None
        and notional_cap
        and notional_after > notional_cap * net_liq
    ):
        errors.append(
            f"Uncovered short options on {t.underlying} would carry "
            f"{notional_after:,.0f} of assignment exposure ({notional_after / net_liq:.0%} of "
            f"net liq{look_through}), above the {notional_cap:.0%} notional limit; use a "
            "defined-risk spread or fewer contracts"
        )
    if t.intent == "open":
        for leg in t.legs:
            if leg.expiration:
                dte = (leg.expiration - today).days
                if dte < 21:
                    warnings.append(f"Opening at {dte} DTE, inside the management window")
                    break
    return errors, warnings


def _fmt(net: float) -> str:
    return f"{abs(net):.2f} {'credit' if net > 0 else 'debit' if net < 0 else 'even'}"
