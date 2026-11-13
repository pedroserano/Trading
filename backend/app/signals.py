"""Signals for selling into a sharp move: put skew, the drop signal, a possible unscheduled
shock, and the one-sided trade sold outside the expected move. Pure functions.

The idea (tastylive): a sharp drop with an IV rank spike and steep put skew is when puts are
most expensive. Don't call the bottom: sell the put side outside the expected move, so the
trade works if the stock rallies, stalls or falls a little further. A shock that isn't a
scheduled report (a CEO leaving, an investigation) means part of the IV is real gap risk:
defined risk and smaller size then.
"""

import math
from datetime import date


def _nearest(rows: list[dict], side: str, target: float) -> dict | None:
    cands = [r for r in rows if r.get(side) and r[side].get("delta") is not None]
    return min(cands, key=lambda r: abs(abs(r[side]["delta"]) - target)) if cands else None


def skew(rows: list[dict], delta: float = 0.25) -> float | None:
    """Put skew in volatility points: the ~25-delta put's IV less the ~25-delta call's."""
    p, c = _nearest(rows, "put", delta), _nearest(rows, "call", delta)
    if not p or not c or p["put"].get("iv") is None or c["call"].get("iv") is None:
        return None
    return round(p["put"]["iv"] - c["call"]["iv"], 2)


def drop_signal(direction: str | None, ivr: float | None, sk: float | None, s: dict) -> bool:
    """A sharp drop, a high IV rank and steep put skew: when puts are dearest."""
    return (
        direction == "down"
        and ivr is not None
        and ivr >= s["drop_ivr"]
        and sk is not None
        and sk >= s["steep_skew"]
    )


def _after_report(day: date, last_report: str | None) -> bool:
    """Within two sessions of the last earnings report (four calendar days covers a
    weekend): a gap then is the report, not a shock."""
    if not last_report:
        return False
    gap = (day - date.fromisoformat(last_report)).days
    return 0 <= gap <= 4


def shock(
    closes: list[tuple[date, float]],
    iv_hist: list[tuple[date, float]],
    last_report: str | None,
    s: dict,
    today_change_pct: float | None = None,
    today: date | None = None,
) -> str | None:
    """Why the underlying may have had an unscheduled shock lately, or None.

    Over the last `shock_sessions` sessions: a day's move at least `shock_day_ratio` times
    the average of the 20 sessions before it, or IV30 up at least `shock_iv_jump` (relative)
    over 5 sessions; either one not within two sessions after an earnings report.
    """
    window = int(s["shock_sessions"])
    found: list[tuple[date, str]] = []
    values = [(d, c) for d, c in closes if c and c > 0]
    rets = [
        (values[i][0], math.log(values[i][1] / values[i - 1][1])) for i in range(1, len(values))
    ]
    if today_change_pct is not None and today is not None and values:
        rets.append((today, math.log(1 + today_change_pct / 100)))
    for i in range(max(20, len(rets) - window), len(rets)):
        day, r = rets[i]
        before = [abs(x) for _, x in rets[i - 20 : i]]
        avg = sum(before) / len(before) if before else 0
        if avg > 0 and abs(r) >= s["shock_day_ratio"] * avg and not _after_report(day, last_report):
            found.append(
                (day, f"{day.isoformat()} move {r * 100:+.1f}%, {abs(r) / avg:.1f}× its average")
            )
    ivs = [(d, v) for d, v in iv_hist if v]
    for j in range(max(5, len(ivs) - window), len(ivs)):
        day, v = ivs[j]
        prior = ivs[j - 5][1]
        if prior and v / prior - 1 >= s["shock_iv_jump"] and not _after_report(day, last_report):
            found.append((day, f"IV30 up {v / prior - 1:.0%} in 5 sessions to {day.isoformat()}"))
    if not found:
        return None
    day, why = max(found)
    return f"possible unscheduled shock ({why}, no earnings report)"


def one_side(
    rows: list[dict],
    spot: float | None,
    em: float | None,
    side: str,
    undefined: bool,
    s: dict,
    mid,
) -> tuple[list[dict] | None, str]:
    """The trade on one side only, outside the expected move: a short put (or call), or a
    credit spread when defined risk is wanted. Legs for one unit, and a label."""
    pc = "put" if side == "put" else "call"
    cands = [
        r
        for r in rows
        if r.get(pc)
        and r[pc].get("delta") is not None
        and r[pc].get("symbol")
        and abs(r[pc]["delta"]) <= s["short_delta"] + 1e-9
    ]
    if spot and em:
        edge = spot - em if pc == "put" else spot + em
        cands = [r for r in cands if (r["strike"] <= edge if pc == "put" else r["strike"] >= edge)]
    if not cands:
        return None, f"no {pc} outside the expected move"
    short = (
        max(cands, key=lambda r: r["strike"])
        if pc == "put"
        else min(cands, key=lambda r: r["strike"])
    )
    where = f"outside the ±{em:.2f} expected move" if em else f"at {s['short_delta']:.2f}Δ"
    kind = "put" if pc == "put" else "call"
    if undefined:
        return [{"symbol": short[pc]["symbol"], "quantity": -1, "effect": "open"}], (
            f"Short {kind} {short['strike']:g} ({where})"
        )
    wings = sorted(
        (
            r
            for r in rows
            if r.get(pc)
            and r[pc].get("symbol")
            and (r["strike"] < short["strike"] if pc == "put" else r["strike"] > short["strike"])
        ),
        key=lambda r: abs(r["strike"] - short["strike"]),
    )
    sm = mid(short[pc])
    for w in wings:
        lm = mid(w[pc])
        width = abs(short["strike"] - w["strike"])
        if sm is None or lm is None or not width:
            continue
        if (sm - lm) / width >= s["min_ic_credit"]:
            label = (
                f"{'Put' if pc == 'put' else 'Call'} spread {w['strike']:g}/{short['strike']:g} "
                f"({where})"
            )
            return [
                {"symbol": w[pc]["symbol"], "quantity": 1, "effect": "open"},
                {"symbol": short[pc]["symbol"], "quantity": -1, "effect": "open"},
            ], label
    return None, (
        f"no {kind} spread outside the expected move collects {s['min_ic_credit']:.0%} of its width"
    )
