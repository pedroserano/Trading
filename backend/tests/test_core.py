from datetime import date, datetime

import pytest
from fake_source import DemoDataSource

from app import core
from app.config import Settings
from app.notify import ET
from app.portfolio import PortfolioService
from app.store import MemoryStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_parse_reads_symbol_and_shares():
    got, bad = core.parse("aapl 200\n# a note\nMSFT, 50\nBRK/B 10\nbad line\nX 0")
    assert got == {"AAPL": 200, "MSFT": 50, "BRK/B": 10}
    assert bad == ["bad line", "X 0"]


async def _resolver():
    return DemoDataSource.HASH


def _portfolio(store):
    s = Settings(
        _env_file=None,
        app_password="p" * 12,
        secret_key="s" * 32,
        schwab_app_key="k",
        schwab_app_secret="s",
        cookie_secure=False,
    )
    return PortfolioService(
        DemoDataSource(today=date(2026, 11, 11)),
        store,
        s,
        lambda: {},
        _resolver,
        "live",
        clock=lambda: datetime(2026, 11, 11, 11, tzinfo=ET),
    )


@pytest.mark.anyio
async def test_core_shares_and_their_calls_leave_the_portfolio_delta():
    store = MemoryStore()
    for sym, b in (("AAPL", 1.2), ("SPY", 1.0), ("QQQ", 1.1), ("IWM", 1.3)):
        await store.upsert_beta(sym, b, date(2026, 11, 11))
    p = _portfolio(store)
    await p.tick()
    aapl = next(u for u in p.snapshot["underlyings"] if u["symbol"] == "AAPL")
    before_u, before_t = aapl["delta_bw"], p.snapshot["totals"]["delta_bw"]
    assert aapl["core"] is None and before_u
    # The demo holds 100 AAPL with a covered call: both are core.
    await store.set_setting(core.KEY, {"live": "AAPL 500"})
    await p.tick()
    aapl = next(u for u in p.snapshot["underlyings"] if u["symbol"] == "AAPL")
    c = aapl["core"]
    assert c["shares"] == 100 and c["calls"] == 1  # no more than are held
    call = next(
        leg
        for st in aapl["strategies"]
        for leg in st["legs"]
        if leg["kind"] == "OPTION" and leg["put_call"] == "C"
    )
    assert c["delta"] == pytest.approx(100 - 100 * call["delta"], abs=0.01)
    # AAPL is left out entirely: its part of the delta is now zero, and the total moves by it.
    assert aapl["delta_bw"] == pytest.approx(0, abs=0.2)
    assert p.snapshot["totals"]["delta_bw"] == pytest.approx(before_t - before_u, abs=0.2)
    # Another account's core shares are not this one's.
    await store.set_setting(core.KEY, {"other": "AAPL 100"})
    await p.tick()
    aapl = next(u for u in p.snapshot["underlyings"] if u["symbol"] == "AAPL")
    assert aapl["core"] is None and aapl["delta_bw"] == pytest.approx(before_u, rel=0.05)
