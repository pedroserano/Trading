import asyncio

from fake_source import DemoDataSource

from app.schwab_service import SchwabError
from app.watchlist import FUNDAMENTALS_REFRESH_S, WatchlistService, float_info

# GME as Schwab returned it: marketCapFloat is a share count, not dollars.
GME = {
    "sharesOutstanding": 504500990.0,
    "marketCapFloat": 460411538.58172,
    "marketCap": 12915225344.0,
    "avg10DaysVolume": 5884347,
    "shortIntToFloat": 0.0,
}


def test_float_is_read_as_shares():
    f = float_info(GME)
    assert round(f["shares"]) == 460411539
    assert round(f["pct_of_outstanding"], 3) == 0.913
    assert round(f["turnover"], 4) == 0.0128
    assert f["low"] is False


def test_small_float_is_flagged_and_funds_have_none():
    assert float_info({"marketCapFloat": 8e6, "sharesOutstanding": 1e7})["low"] is True
    assert float_info({"marketCapFloat": 0.0, "sharesOutstanding": 0.0}) is None
    assert float_info(None) is None


class _Portfolio:
    watchlist = ["AAPL", "XYZ", "$SPX"]


def test_watchlist_fetches_floats_once_a_day_and_skips_indexes():
    src = DemoDataSource()
    asked = []
    real = src.fundamentals

    async def spy(symbols):
        asked.append(list(symbols))
        return await real(symbols)

    src.fundamentals = spy
    w = WatchlistService(src, None, _Portfolio())
    asyncio.run(w._refresh_fundamentals(1000.0, False))
    asyncio.run(w._refresh_fundamentals(1000.0 + 3600, False))
    assert asked == [["AAPL", "XYZ"]]
    assert w.fundamentals["XYZ"]["float"]["low"] is True
    assert w.fundamentals["AAPL"]["float"]["low"] is False
    asyncio.run(w._refresh_fundamentals(1000.0 + FUNDAMENTALS_REFRESH_S, False))
    assert len(asked) == 2


def test_a_failed_fetch_keeps_the_last_float():
    src = DemoDataSource()
    w = WatchlistService(src, None, _Portfolio())
    asyncio.run(w._refresh_fundamentals(0.0, False))
    before = w.fundamentals["AAPL"]["float"]

    async def boom(symbols):
        raise SchwabError("down")

    src.fundamentals = boom
    asyncio.run(w._refresh_fundamentals(FUNDAMENTALS_REFRESH_S, False))
    assert w.fundamentals["AAPL"]["float"] == before


def test_a_long_watchlist_refreshes_its_chains_a_few_at_a_time():
    from datetime import date

    from app import watchlist as wl

    class Many:
        watchlist = [f"S{i:03d}" for i in range(35)]

    calls = []

    class Src(DemoDataSource):
        async def option_chain(self, sym, *a, **k):
            calls.append(sym)
            return {}

    w = WatchlistService(Src(), None, Many())
    asyncio.run(w.refresh(date(2026, 11, 19)))
    assert len(calls) == wl.CHAINS_PER_PASS
    asyncio.run(w.refresh(date(2026, 11, 19)))
    assert len(calls) == 2 * wl.CHAINS_PER_PASS and len(set(calls)) == len(calls)
    asyncio.run(w.refresh(date(2026, 11, 19), force=True))
    assert len(calls) == 2 * wl.CHAINS_PER_PASS + 35
