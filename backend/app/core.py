"""Core shares: stock held apart from options trading, left out of the portfolio's delta.

Per account, lines of SYMBOL SHARES (e.g. "AAPL 200"). Up to that many long shares of the
symbol, and the covered calls written on them (one per 100), are left out of the
beta-weighted delta: the header's figure, the delta band, the scanner's lean and automanage's
portfolio view. Shares above the number still count, as do every other option on the
symbol. Net liq, buying power and P/L are unchanged.
"""

from __future__ import annotations

import re

from .strategies import COVERED_KINDS

KEY = "core_shares"  # {account key: text}


def parse(text: str) -> tuple[dict[str, int], list[str]]:
    """{SYMBOL: shares} from the text, and the lines that could not be read."""
    out: dict[str, int] = {}
    bad: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"([A-Za-z./]+)[\s,]+(\d+)", line)
        if not m or int(m.group(2)) <= 0:
            bad.append(line)
            continue
        out[m.group(1).upper()] = out.get(m.group(1).upper(), 0) + int(m.group(2))
    return out, bad


def excluded(strategies, core: int) -> dict | None:
    """What is left out for one underlying holding `core` core shares: the shares (no more
    than are held long) and up to one covered call per 100 of them, with their delta in
    shares. None when nothing is; delta None when a call's delta is unknown."""
    if core <= 0:
        return None
    held = sum(
        leg.quantity
        for st in strategies
        for leg in st.legs
        if not leg.is_option and leg.quantity > 0
    )
    shares = min(core, held)
    if shares <= 0:
        return None
    delta: float | None = float(shares)
    room = shares // 100  # calls the core shares cover
    calls = 0
    for st in strategies:
        if st.name not in COVERED_KINDS or room <= 0:
            continue
        for leg in st.legs:
            if not (leg.is_option and leg.put_call == "C" and leg.quantity < 0) or room <= 0:
                continue
            n = min(-leg.quantity, room)
            room -= n
            calls += n
            if leg.delta is None or delta is None:
                delta = None
            else:
                delta -= n * leg.delta * leg.multiplier
    return {"shares": shares, "calls": calls, "delta": None if delta is None else round(delta, 2)}
