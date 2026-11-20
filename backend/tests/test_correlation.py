"""Correlation of underlyings, for spreading risk across them."""

import math
from datetime import date, timedelta

import pytest

from app import correlation as cr


@pytest.fixture
def anyio_backend():
    return "asyncio"


def series(f, n=120):
    d0 = date(2026, 1, 1)
    return [(d0 + timedelta(days=i), 100.0 * f(i)) for i in range(n)]


A = series(lambda i: 1 + 0.02 * math.sin(i) + 0.001 * i)
B = series(lambda i: 1 + 0.03 * math.sin(i) + 0.001 * i)  # moves with A
C = series(lambda i: 1 + 0.02 * math.cos(i * 1.7))  # does not


def test_correlation_of_daily_returns():
    assert cr.correlation(A, B) > 0.95
    assert abs(cr.correlation(A, C)) < cr.HIGH
    assert cr.correlation(A[:30], B[:30]) is None  # too few returns


class Source:
    def __init__(self, data):
        self.data, self.calls = data, 0

    async def daily_closes(self, symbol, days):
        self.calls += 1
        if symbol == "BAD":
            raise RuntimeError("no history")
        return self.data.get(symbol, [])


@pytest.mark.anyio
async def test_moves_with_big_holdings_and_cached_closes():
    src = Source({"SPY": A, "QQQ": B, "GLD": C})
    c = cr.Correlations(src, lambda: date(2026, 11, 11))
    big = cr.big_underlyings({"QQQ": 12_000.0, "GLD": 9_000.0, "XLE": 1_000.0}, 100_000.0)
    assert big == {"QQQ": 0.12, "GLD": 0.09}  # XLE is under 5%
    found = await c.moves_with("SPY", big)
    assert [(u, s) for u, _, s in found] == [("QQQ", 0.12)]
    calls = src.calls
    await c.moves_with("SPY", big)
    assert src.calls == calls  # closes read once a day per symbol
    w = cr.warning("SPY", found)
    assert w.startswith("SPY has moved with QQQ (") and "67%" in w
    assert cr.warning("SPY", []) is None
    # A failed read is no correlation, never an error.
    assert await c.moves_with("BAD", big) == []
