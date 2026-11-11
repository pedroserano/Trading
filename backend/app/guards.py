"""Checks every order must pass before it is sent anywhere. Pure functions.

Errors block the order. Warnings are shown on the ticket and need no override.
"""

from dataclasses import dataclass
from datetime import date

from .orders import Ticket


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
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for leg in t.legs:
        if abs(leg.quantity) > limits.max_contracts:
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
    if bp_used_after is not None and bp_ceiling is not None and bp_used_after > bp_ceiling:
        warnings.append(
            f"Buying power used would be {bp_used_after:.0%}, above the "
            f"{bp_ceiling:.0%} ceiling for the current VIX"
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
