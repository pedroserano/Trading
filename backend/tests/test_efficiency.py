"""The caches that save repeated work: they must never serve stale data."""

import json
from contextlib import asynccontextmanager
from datetime import date

import pytest
from test_services import demo_portfolio

from app import datasource
from app.automation import AutomationService
from app.datasource import SchwabDataSource
from app.store import PgStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return self.rows


class FakeDb:
    """Stands in for Postgres: a settings table, iv_history and betas, counting queries."""

    def __init__(self):
        self.settings: dict[str, str] = {}
        self.iv: list[tuple[str, date, float]] = []
        self.betas: dict[str, tuple[float, date]] = {}
        self.selects = 0

    async def execute(self, sql, args=()):
        if sql.startswith("SELECT"):
            self.selects += 1
        if sql.startswith("SELECT value FROM settings"):
            v = self.settings.get(args[0])
            return FakeCursor([] if v is None else [(json.loads(v),)])
        if sql.startswith("INSERT INTO settings"):
            self.settings[args[0]] = args[1]
        if sql.startswith("SELECT day, iv30"):
            sym, since = args
            return FakeCursor(sorted((d, v) for s, d, v in self.iv if s == sym and d >= since))
        if sql.startswith("INSERT INTO iv_history"):
            self.iv.append((args[0], args[1], args[2]))
        if sql.startswith("SELECT beta"):
            b = self.betas.get(args[0])
            return FakeCursor([b] if b else [])
        if sql.startswith("INSERT INTO betas"):
            self.betas[args[0]] = (args[1], args[2])
        return FakeCursor([])

    @asynccontextmanager
    async def connection(self, timeout=None):
        yield self


def pg() -> tuple[PgStore, FakeDb]:
    store = PgStore("h", 5432, "d", "u", "p")
    db = FakeDb()
    store._pool = db
    return store, db


@pytest.mark.anyio
async def test_settings_are_read_once_and_follow_every_write():
    store, db = pg()
    assert await store.get_setting("rules") is None
    assert await store.get_setting("rules") is None
    assert db.selects == 1  # a missing row is remembered too
    await store.set_setting("rules", {"manage_dte": 21})
    got = await store.get_setting("rules")
    assert got == {"manage_dte": 21} and db.selects == 1
    got["manage_dte"] = 99  # a caller changing its copy does not change the stored value
    assert await store.get_setting("rules") == {"manage_dte": 21}


@pytest.mark.anyio
async def test_iv_history_and_betas_are_cached_until_written():
    store, db = pg()
    await store.upsert_iv("AAPL", date(2026, 11, 2), 30.0, "chain")
    year = await store.iv_history("AAPL", date(2025, 11, 1))
    recent = await store.iv_history("AAPL", date(2026, 11, 1))
    assert year == recent == [(date(2026, 11, 2), 30.0)] and db.selects == 1
    await store.upsert_iv("AAPL", date(2026, 11, 3), 31.0, "chain")
    assert len(await store.iv_history("AAPL", date(2026, 11, 1))) == 2  # read again
    # An earlier start than the cached one reads again.
    before = db.selects
    await store.iv_history("AAPL", date(2020, 1, 1))
    assert db.selects == before + 1

    assert await store.get_beta("AAPL") is None
    assert await store.get_beta("AAPL") is None
    n = db.selects
    await store.upsert_beta("AAPL", 1.2, date(2026, 11, 3))
    assert await store.get_beta("AAPL") == (1.2, date(2026, 11, 3)) and db.selects == n


class FakeSchwab:
    def __init__(self):
        self.calls: list[list[str]] = []

    async def call(self, name, symbols):
        self.calls.append(list(symbols))
        return {s: {"symbol": s, "quote": {"mark": 1.0}} for s in symbols}


@pytest.mark.anyio
async def test_quotes_fetched_moments_ago_are_shared(monkeypatch):
    schwab = FakeSchwab()
    src = SchwabDataSource(schwab)
    clock = [100.0]
    monkeypatch.setattr(datasource.time, "monotonic", lambda: clock[0])
    a = await src.quotes(["SPY", "$VIX", "AAPL"])
    b = await src.quotes(["SPY", "$VIX", "QQQ"])  # another account, a moment later
    assert schwab.calls == [["SPY", "$VIX", "AAPL"], ["QQQ"]]
    assert set(b) == {"SPY", "$VIX", "QQQ"}
    a["SPY"]["quote"]["mark"] = 9.0  # one caller's update does not reach another's copy
    assert b["SPY"]["quote"]["mark"] == 1.0
    clock[0] += datasource.QUOTE_TTL_S + 0.1
    await src.quotes(["SPY"])
    assert schwab.calls[-1] == ["SPY"]  # stale: fetched again


@pytest.mark.anyio
async def test_snapshot_is_serialised_once_per_snapshot():
    p, _, _ = demo_portfolio()
    await p.tick()
    q1, q2 = p.subscribe(), p.subscribe()
    p._publish()
    t1, t2 = q1.get_nowait(), q2.get_nowait()
    assert t1 is t2 and json.loads(t1)["underlyings"]
    await p.tick()
    assert p.snapshot_text() is not t1  # a new snapshot, a new text


@pytest.mark.anyio
async def test_automation_view_can_leave_out_the_log():
    p, source, store = demo_portfolio()

    class Broker:
        state = None

    auto = AutomationService(store, p, Broker(), source, None, None, None, account="sim")
    await auto.load()
    auto._log(None, None, "something happened")
    assert (await auto.view())["log"]
    assert (await auto.view(log=False))["log"] == []
