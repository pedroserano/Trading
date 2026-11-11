from datetime import date

import pytest

from app import market as mk


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize(
    "root,today,expected",
    [
        ("ES", date(2026, 11, 11), "/ESZ26"),
        ("ES", date(2026, 12, 9), "/ESZ26"),  # the day before the roll
        ("ES", date(2026, 12, 10), "/ESH27"),  # 8 days before the third Friday (Dec 18)
        ("ZN", date(2027, 2, 1), "/ZNH27"),
        ("CL", date(2026, 11, 11), "/CLZ26"),
        ("CL", date(2026, 11, 18), "/CLF27"),  # 5 business days before Nov 25
        ("GC", date(2026, 11, 11), "/GCZ26"),
        ("GC", date(2026, 11, 25), "/GCG27"),
        ("SI", date(2026, 11, 11), "/SIZ26"),
        ("BTC", date(2026, 11, 11), "/BTCX26"),
        ("BTC", date(2026, 11, 22), "/BTCZ26"),  # 5 days before the last Friday (Nov 27)
    ],
)
def test_front_contract_rolls(root, today, expected):
    assert mk.front_contract(root, today) == expected


def test_items_read_points_and_percent_the_same_way():
    es = {
        "symbol": "/ES",
        "reference": {"futureActiveSymbol": "/ESZ26"},
        "quote": {"lastPrice": 5930.0, "netChange": 25.0},
    }
    i = mk.item("S&P", "/ES", es)
    assert i["contract"] == "/ESZ26" and i["change_pct"] == pytest.approx(0.42, abs=0.01)
    spx = {"quote": {"lastPrice": 5900.0, "netPercentChange": -0.3}}
    assert mk.item("S&P", "$SPX", spx, fallback=True)["change_pct"] == -0.3
    empty = mk.item("Gold", "/GC", None)
    assert empty["price"] is None and empty["change_pct"] is None


class Source:
    """Quotes only what it is told it can."""

    def __init__(self, known):
        self.known = known
        self.asked = []

    async def quotes(self, symbols):
        self.asked.append(list(symbols))
        return {
            s: {"quote": {"lastPrice": 100.0, "netChange": 1.0}} for s in symbols if s in self.known
        }


@pytest.mark.anyio
async def test_roots_then_contracts_then_stand_ins():
    from datetime import datetime

    from app.notify import ET

    clock = lambda: datetime(2026, 11, 11, 10, tzinfo=ET)  # noqa: E731
    # Roots resolve: one request, every future quoted directly.
    roots = {s for _, s, _ in mk.MARKETS}
    svc = mk.MarketService(Source(roots), clock)
    items = await svc.refresh()
    assert all(i["price"] == 100.0 and not i["fallback"] for i in items)
    assert len(svc.source.asked) == 1
    # Roots not resolved, contracts are: each future quoted by its front contract.
    contracts = {
        mk.front_contract(s[1:], date(2026, 11, 11)) for _, s, _ in mk.MARKETS if s.startswith("/")
    }
    svc = mk.MarketService(Source(contracts | {"$VIX"}), clock)
    items = {i["label"]: i for i in await svc.refresh()}
    assert items["S&P"]["contract"] == "/ESZ26" and items["S&P"]["price"] == 100.0
    # Neither: cash stand-ins, marked as such.
    svc = mk.MarketService(Source({"$SPX", "GLD", "$VIX"}), clock)
    items = {i["label"]: i for i in await svc.refresh()}
    assert items["S&P"]["symbol"] == "$SPX" and items["S&P"]["fallback"] is True
    assert items["Gold"]["symbol"] == "GLD" and items["Crude"]["price"] is None
    # Later passes skip what did not work.
    svc.source.asked.clear()
    await svc.refresh()
    assert "/ES" not in svc.source.asked[0]
