import asyncio
from datetime import date

import pytest

from app import datasource as ds
from app.pacing import Pacer, background, is_background, run_in_background
from app.schwab_service import SchwabNotConnected, SchwabService, _market_data


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_pacer_holds_requests_over_the_limit_until_the_window_moves():
    t = [0.0]
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        t[0] += s

    p = Pacer(per_window=3, window=60, clock=lambda: t[0])
    real = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        for _ in range(3):
            await p.acquire()
        assert slept == []
        t[0] = 10
        await p.acquire()  # the fourth waits for the first to leave the window
    finally:
        asyncio.sleep = real
    assert len(slept) == 1 and slept[0] == pytest.approx(50.01)
    assert len(p.sent) == 1  # the first three, all at 0, have left the window


@pytest.mark.anyio
async def test_background_marks_its_tasks():
    seen = []

    async def job():
        seen.append(is_background())
        seen.append(await asyncio.create_task(asyncio.sleep(0, result=is_background())))

    assert not is_background()
    await run_in_background(job)
    assert seen == [True, True] and not is_background()


def test_only_market_data_is_paced():
    assert _market_data("get_option_chain") and _market_data("get_price_history_every_day")
    assert _market_data("get_quotes") and _market_data("get_instruments")
    assert not _market_data("place_order") and not _market_data("get_account")
    assert not _market_data("get_orders_for_account")


@pytest.mark.anyio
async def test_background_market_data_waits_for_the_pacer():
    svc = SchwabService.__new__(SchwabService)
    calls = []

    class P:
        async def acquire(self):
            calls.append("paced")

    svc._pacer = P()
    svc._usable = lambda: True

    class Resp:
        status_code = 200
        content = b""

    async def send(method, *a, **k):
        calls.append(method)
        return Resp()

    svc._send = send
    svc._soft_401s = 0
    svc._auth_failure = None
    await svc._request("get_option_chain", "SPY")
    await svc._request("place_order", "h", {})
    with background():
        await svc._request("get_option_chain", "SPY")
        await svc._request("get_account", "h")
    assert calls == ["get_option_chain", "place_order", "paced", "get_option_chain", "get_account"]


@pytest.mark.anyio
async def test_accounts_without_a_sign_in_is_not_connected():
    svc = SchwabService.__new__(SchwabService)
    svc._client = None
    with pytest.raises(SchwabNotConnected):
        await svc.accounts()


class _Schwab:
    def __init__(self):
        self.calls = []

    def client(self):
        class C:
            class Options:
                class ContractType:
                    ALL = "ALL"

        return C

    async def call(self, method, *args, **kwargs):
        self.calls.append(method)
        return {"symbol": args[0], "n": len(self.calls)}


@pytest.mark.anyio
async def test_background_chains_are_shared_and_foreground_ones_are_fresh():
    sch = _Schwab()
    src = ds.SchwabDataSource(sch)
    d = date(2026, 12, 18)
    with background():
        a = await src.option_chain("SPY", d, d, None)
        b = await src.option_chain("SPY", d, d, None)  # the second account's scan
        c = await src.option_chain("SPY", d, d, 6)  # another shape is its own request
    assert a is b and c is not a
    fresh = await src.option_chain("SPY", d, d, None)
    assert fresh is not a
    assert sch.calls.count("get_option_chain") == 3


@pytest.mark.anyio
async def test_old_background_chains_are_fetched_again(monkeypatch):
    sch = _Schwab()
    src = ds.SchwabDataSource(sch)
    t = [1000.0]
    monkeypatch.setattr(ds.time, "monotonic", lambda: t[0])
    d = date(2026, 12, 18)
    with background():
        a = await src.option_chain("SPY", d, d, None)
        t[0] += ds.CHAIN_TTL_S + 1
        b = await src.option_chain("SPY", d, d, None)
    assert a is not b


@pytest.mark.anyio
async def test_chain_cache_is_bounded(monkeypatch):
    sch = _Schwab()
    src = ds.SchwabDataSource(sch)
    monkeypatch.setattr(ds, "CHAIN_CACHE_MAX", 3)
    d = date(2026, 12, 18)
    with background():
        for sym in ("A", "B", "C", "D"):
            await src.option_chain(sym, d, d, None)
    assert len(src._chains) == 3 and ("A", d, d, None) not in src._chains


@pytest.mark.anyio
async def test_expirations_are_reused_for_half_an_hour(monkeypatch):
    sch = _Schwab()
    src = ds.SchwabDataSource(sch)
    t = [1000.0]
    monkeypatch.setattr(ds.time, "monotonic", lambda: t[0])
    await src.expirations("SPY")
    await src.expirations("SPY")
    assert sch.calls.count("get_option_expiration_chain") == 1
    t[0] += ds.EXPIRATIONS_TTL_S + 1
    await src.expirations("SPY")
    assert sch.calls.count("get_option_expiration_chain") == 2


@pytest.mark.anyio
async def test_a_scan_goes_ahead_of_routine_refreshes_and_counts_its_waits():
    from app.pacing import count_waits, priority

    t = [0.0]
    real = asyncio.sleep

    async def fake_sleep(s):
        t[0] += s
        for _ in range(3):  # time for others waiting to take their turn
            await real(0)

    p = Pacer(per_window=2, window=60, clock=lambda: t[0])
    asyncio.sleep = fake_sleep
    order = []
    try:
        await p.acquire()
        await p.acquire()  # the window is full

        async def routine(name):
            await p.acquire()
            order.append(name)

        async def scan(name, into):
            with priority(), count_waits(into):
                await p.acquire()
            order.append(name)

        waited = [0.0, 0]
        first = asyncio.create_task(routine("watchlist"))
        await real(0)  # the refresh queues first
        second = asyncio.create_task(scan("scan", waited))
        await asyncio.gather(first, second)
    finally:
        asyncio.sleep = real
    assert order == ["scan", "watchlist"]
    assert waited[1] == 1  # one paced request counted

    # A full window: the wait is counted.
    q = Pacer(per_window=1, window=60, clock=lambda: t[0])
    asyncio.sleep = fake_sleep
    try:
        await q.acquire()
        waited = [0.0, 0]
        with count_waits(waited):
            await q.acquire()
    finally:
        asyncio.sleep = real
    assert waited[1] == 1 and waited[0] >= 60
