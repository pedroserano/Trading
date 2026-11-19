import asyncio
import re
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

import httpx

from app import ivref
from app.ivref import IvReference, choose, rank_from
from app.store import MemoryStore

TODAY = date(2026, 11, 19)


def clock():
    return datetime(2026, 11, 19, 20, 0, tzinfo=ivref.ET)


def row(sym, day, iv, hi, lo):
    return {
        "act_symbol": sym,
        "date": day,
        "iv_current": None if iv is None else str(iv),
        "iv_year_high": None if hi is None else str(hi),
        "iv_year_low": None if lo is None else str(lo),
    }


# What the fake DoltHub holds: Nov 19 is not in yet, so Nov 18 is the latest day. On Nov 18
# AAPL lacks its year range and is read from Nov 17.
DATA = [
    row("SPY", "2026-11-18", 0.2027, 0.2648, 0.1090),
    row("SPY", "2026-11-17", 0.2070, 0.2648, 0.1090),
    row("SPY", "2026-11-16", 0.1870, 0.2648, 0.1090),
    row("AAPL", "2026-11-18", 0.2438, None, None),
    row("AAPL", "2026-11-17", 0.2542, 0.3246, 0.1748),
    row("BRK.B", "2026-11-18", 0.1500, 0.2500, 0.1000),
]


def fake_dolthub(queries):
    def handler(request: httpx.Request):
        sql = parse_qs(urlparse(str(request.url)).query)["q"][0]
        queries.append(sql)
        day = re.search(r"date = '([\d-]+)'", sql).group(1)
        syms = re.findall(r"'([A-Z.]+)'", sql.split("act_symbol", 1)[1])
        rows = [r for r in DATA if r["date"] == day and r["act_symbol"] in syms]
        if sql.startswith("SELECT date "):
            rows = [{"date": r["date"]} for r in rows]
        return httpx.Response(200, json={"query_execution_status": "Success", "rows": rows})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_rank_is_dolthubs_iv_against_its_own_year():
    r = rank_from({"date": "2026-11-18", "iv": 0.2027, "high": 0.2648, "low": 0.1090})
    assert r["rank"] == 60.1 and r["low"] == 10.9 and r["high"] == 26.48 and r["iv"] == 20.27
    assert r["source"] == "DoltHub" and r["as_of"] == "2026-11-18" and r["percentile"] is None
    assert rank_from({"date": "2026-11-18", "iv": 0.2, "high": None, "low": 0.1}) is None


def test_our_own_history_takes_over_after_a_year():
    ref = rank_from({"date": "2026-11-18", "iv": 0.2, "high": 0.3, "low": 0.1})
    own = {"rank": 70.0, "percentile": 80.0, "days": 30, "source": "recorded"}
    # Under a year of our own: DoltHub's, noting how much of ours exists.
    got = choose(own, date(2026, 10, 1), ref, TODAY)
    assert got["source"] == "DoltHub" and got["recorded_days"] == 30
    # A year of our own: ours.
    assert choose(own, date(2025, 11, 19), ref, TODAY) is own
    # DoltHub stale (it stopped updating) or missing: ours, as before.
    old = {**ref, "as_of": "2026-11-01"}
    assert choose(own, date(2026, 10, 1), old, TODAY) is own
    assert choose(own, date(2026, 10, 1), None, TODAY) is own
    assert choose(None, None, ref, TODAY)["source"] == "DoltHub"


def test_refresh_reads_the_latest_day_in_batches_and_fills_gaps():
    queries: list[str] = []
    store = MemoryStore()

    async def symbols():
        return ["SPY", "AAPL", "BRK/B", "$VIX", "/ES", "NOPE"]

    async def go():
        ref = IvReference(store, symbols, clock=clock, client=fake_dolthub(queries))
        n = await ref.refresh()
        assert n == 3
        assert ref.rank("SPY")["rank"] == 60.1
        assert ref.rank("AAPL")["as_of"] == "2026-11-17"  # the day before had the range
        assert ref.rank("BRK/B")["rank"] == 33.3  # BRK.B at DoltHub
        assert ref.rank("NOPE") is None and ref.rank("$VIX") is None
        assert ref.status()["symbols"] == 3 and ref.status()["as_of"] == "2026-11-18"
        # Kept across a restart.
        again = IvReference(store, symbols, clock=clock)
        await again.load()
        assert again.rank("SPY")["rank"] == 60.1

    asyncio.run(go())
    # Indexes and futures are never asked for; every data query names a single day.
    assert not any("VIX" in q or "/ES" in q for q in queries)
    assert all("date = '" in q for q in queries)
    # Weekends are never asked for, and no more than three trading days are read.
    asked = {re.search(r"date = '([\d-]+)'", q).group(1) for q in queries}
    assert asked == {"2026-11-19", "2026-11-18", "2026-11-17", "2026-11-16"}


def test_refresh_does_nothing_in_demo():
    async def symbols():
        return ["SPY"]

    async def go():
        ref = IvReference(MemoryStore(), symbols, clock=clock, demo=True)
        assert await ref.refresh() == 0 and ref.rank("SPY") is None

    asyncio.run(go())
