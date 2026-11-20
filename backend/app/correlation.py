"""How closely underlyings move together, for spreading risk across them.

The Unlucky Investor's Guide to Options Trading (Julia Spina): diversifying the underlyings is
the most important defence against outlier losses. In its data, when a SPY strangle took an
outlier loss a QQQ strangle did too 67% of the time; gold and bonds (GLD, TLT) rarely did.
Correlation of daily returns over the past year stands in for "moves together".
"""

from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger(__name__)

# At or above this, two underlyings count as moving together. The book gives no number; 0.7 is
# a common cut-off for a strong correlation (SPY and QQQ run about 0.9).
HIGH = 0.7
# An existing underlying matters once it holds this share of net liq in buying power.
BIG_SHARE = 0.05
DAYS = 370


def correlation(a: list[tuple[date, float]], b: list[tuple[date, float]]) -> float | None:
    """Correlation of daily returns over the dates both have; None under 60 returns."""
    x, y = dict(a), dict(b)
    days = sorted(set(x) & set(y))
    ra, rb = [], []
    for d0, d1 in zip(days, days[1:], strict=False):
        if x[d0] and y[d0]:
            ra.append(x[d1] / x[d0] - 1)
            rb.append(y[d1] / y[d0] - 1)
    if len(ra) < 60:
        return None
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((p - ma) * (q - mb) for p, q in zip(ra, rb, strict=True))
    va = sum((p - ma) ** 2 for p in ra)
    vb = sum((q - mb) ** 2 for q in rb)
    return None if va == 0 or vb == 0 else cov / (va * vb) ** 0.5


class Correlations:
    """Daily closes fetched at most once a day per symbol, and correlations from them."""

    def __init__(self, source, today=lambda: date.today()):
        self.source = source
        self.today = today
        self._closes: dict[str, tuple[date, list]] = {}

    async def closes(self, symbol: str) -> list[tuple[date, float]]:
        day = self.today()
        have = self._closes.get(symbol)
        if have and have[0] == day:
            return have[1]
        try:
            got = await self.source.daily_closes(symbol, DAYS) or []
        except Exception as e:  # a refinement: never stops a preview or a scan
            log.warning("Closes for %s not read: %s", symbol, e)
            got = []
        self._closes[symbol] = (day, got)
        return got

    async def between(self, a: str, b: str) -> float | None:
        return correlation(await self.closes(a), await self.closes(b))

    async def moves_with(
        self, symbol: str, big: dict[str, float]
    ) -> list[tuple[str, float, float]]:
        """(underlying, correlation, its buying power share) for each big underlying held,
        other than this one, that moves with it; strongest first."""
        out = []
        for other, share in big.items():
            if other == symbol:
                continue
            c = await self.between(symbol, other)
            if c is not None and c >= HIGH:
                out.append((other, round(c, 2), share))
        return sorted(out, key=lambda r: -r[1])


def big_underlyings(bp_by_underlying: dict[str, float], net_liq: float | None) -> dict[str, float]:
    """The underlyings holding at least BIG_SHARE of net liq in buying power, with that share."""
    if not net_liq:
        return {}
    return {
        u: bp / net_liq for u, bp in bp_by_underlying.items() if bp and bp / net_liq >= BIG_SHARE
    }


def warning(symbol: str, matches: list[tuple[str, float, float]]) -> str | None:
    if not matches:
        return None
    parts = ", ".join(f"{u} ({c:.2f}, {s:.0%} of net liq)" for u, c, s in matches)
    return (
        f"{symbol} has moved with {parts} over the past year, already holding that "
        "buying power: an outlier move would likely hit both. The Unlucky Investor's Guide: "
        "SPY and QQQ strangles took outlier losses together 67% of the time; spread risk "
        "across underlyings that move differently"
    )
