from datetime import date, datetime

import pytest

from app.accounts import AccountRegistry
from app.config import Settings
from app.datasource import DemoDataSource
from app.jobs import DailyJobs
from app.notify import ET, ReminderService
from app.portfolio import NeedsSetup, PortfolioService, market_session
from app.schwab_service import ConnectionStatus, SchwabNotConnected
from app.store import MemoryStore

TODAY = date(2026, 11, 11)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def settings(**kw):
    return Settings(
        _env_file=None, app_password="p" * 12, secret_key="s" * 32, demo_mode=True, **kw
    )


def demo_portfolio(store=None, resolver=None):
    source = DemoDataSource(today=TODAY)
    store = store or MemoryStore()
    clock = lambda: datetime(2026, 11, 11, 11, 0, tzinfo=ET)  # noqa: E731
    if resolver is None:

        async def resolver():
            return DemoDataSource.HASH

    p = PortfolioService(
        source, store, settings(), lambda: {"connected": True}, resolver, "demo", clock=clock
    )
    return p, source, store


@pytest.mark.anyio
async def test_demo_snapshot_has_grouped_positions_and_totals():
    p, _, _ = demo_portfolio()
    await p.tick()
    snap = p.snapshot
    assert snap["needs"] is None and snap["error"] is None
    assert snap["session"] == "open"
    assert snap["account"]["last4"] == "1234"
    assert 0 < snap["account"]["bp_used_pct"] < 1
    by_symbol = {u["symbol"]: u for u in snap["underlyings"]}
    assert set(by_symbol) == {"AAPL", "IWM", "QQQ", "SPY"}
    spy = sorted(s["name"] for s in by_symbol["SPY"]["strategies"])
    assert spy == ["Put vertical", "Strangle"]
    assert [s["name"] for s in by_symbol["QQQ"]["strategies"]] == ["Iron condor"]
    assert {s["name"] for s in by_symbol["AAPL"]["strategies"]} == {"Covered call"}
    # SPY rank comes from VIX history; others have no recorded IV yet.
    assert by_symbol["SPY"]["ivr"]["source"] == "VIX"
    assert by_symbol["IWM"]["ivr"] is None
    # Without betas for the other underlyings the portfolio beta-weighted delta is unknown.
    assert by_symbol["SPY"]["delta_bw"] == by_symbol["SPY"]["delta"]
    assert snap["totals"]["delta_bw"] is None
    assert snap["totals"]["theta"] > 0  # net short premium
    assert snap["totals"]["flags"] >= 2


@pytest.mark.anyio
async def test_jobs_record_iv_and_betas_then_snapshot_uses_them():
    p, source, store = demo_portfolio()
    await p.tick()
    jobs = DailyJobs(source, store, p, settings())
    results = await jobs.record_iv(TODAY)
    assert set(results) == {"AAPL", "IWM", "QQQ", "SPY"}
    assert all(v and 5 < v < 80 for v in results.values())
    await jobs.refresh_betas(TODAY)
    assert set(store.betas) == {"AAPL", "IWM", "QQQ"}
    await p.tick()
    assert p.snapshot["totals"]["delta_bw"] is not None
    iwm = next(u for u in p.snapshot["underlyings"] if u["symbol"] == "IWM")
    assert iwm["ivr"]["days"] == 1 and iwm["ivr"]["source"] == "recorded"
    assert iwm["ivr"]["rank"] is None  # too little history to rank


@pytest.mark.anyio
async def test_iv_recorded_once_per_day_after_close():
    p, source, store = demo_portfolio()
    jobs = DailyJobs(source, store, p, settings())
    await jobs.run_once(datetime(2026, 11, 11, 15, 0, tzinfo=ET))
    assert store.iv == {}
    await jobs.run_once(datetime(2026, 11, 11, 16, 30, tzinfo=ET))
    assert date(2026, 11, 11) in store.iv["SPY"]
    assert await store.get_setting("iv_recorded_on") == "2026-11-11"


class Numbers:
    def __init__(self, nums, names=None):
        self.nums, self.names = nums, names or {}

    async def account_numbers(self):
        return [{"accountNumber": n, "hashValue": f"H{n}"} for n in self.nums]

    async def account_names(self):
        return self.names


@pytest.mark.anyio
async def test_the_registry_keeps_the_old_account_as_live_and_names_every_other():
    store = MemoryStore()
    # The account chosen before several were supported stays "live", whatever the order.
    store.settings["account"] = {"number": "22225678", "last4": "5678"}
    src = Numbers(["11111234", "22225678"], {"11111234": "Individual", "22225678": "Joint"})
    reg = AccountRegistry(store, src, lambda: ConnectionStatus(True, created_at=5))
    added, gone = await reg.refresh()
    assert {a["key"]: a["name"] for a in reg.in_use()} == {
        "live": "Joint",
        "live-1234": "Individual",
    }
    assert set(added) == {"live-1234"} and gone == []
    assert await reg.resolver("live-1234")() == "H11111234"
    # Unticked at the next sign-in: out of use, its data kept; ticked again: back as before.
    src.nums = ["22225678"]
    assert await reg.refresh() == ([], ["live-1234"])
    assert reg.keys() == ["live"]
    with pytest.raises(NeedsSetup, match="Individual is not visible"):
        await reg.resolver("live-1234")()
    src.nums = ["11111234", "22225678"]
    assert await reg.refresh() == (["live-1234"], [])
    # Without a nickname: "Account …1234"; two with one name: told apart by the last 4.
    src.names = {"11111234": "", "22225678": ""}
    reg.entry("live-1234")["name"] = ""
    reg.entry("live")["name"] = ""
    assert reg.name("live-1234") == "Account …1234"
    reg.entry("live-1234")["name"] = reg.entry("live")["name"] = "Trading"
    assert reg.name("live") == "Trading …5678"
    # A fresh install: the first account listed is "live".
    fresh = AccountRegistry(MemoryStore(), Numbers(["33339999"]), lambda: ConnectionStatus(True))
    await fresh.refresh()
    assert fresh.keys() == ["live"] and fresh.name("live") == "Account …9999"


@pytest.mark.anyio
async def test_portfolio_reports_what_setup_is_missing():
    async def not_connected():
        raise SchwabNotConnected()

    p, _, _ = demo_portfolio(resolver=not_connected)
    await p.tick()
    assert p.snapshot["needs"]["what"] == "connect"
    assert p.snapshot["underlyings"] == []


def test_market_session():
    assert market_session(datetime(2026, 11, 11, 10, 0, tzinfo=ET), None) == "open"
    assert market_session(datetime(2026, 11, 11, 9, 0, tzinfo=ET), None) == "closed"
    assert market_session(datetime(2026, 11, 14, 11, 0, tzinfo=ET), None) == "closed"
    assert market_session(datetime(2026, 11, 26, 11, 0, tzinfo=ET), False) == "closed"


class Notifier:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send(self, title, message, url="", priority=0):
        self.sent.append(message)


@pytest.mark.anyio
async def test_reminders_are_sent_once():
    store, n = MemoryStore(), Notifier()
    st = ConnectionStatus(True, token_age_s=6.2 * 86400, created_at=1)
    svc = ReminderService(n, store, lambda: st, settings(), demo=False)
    now = datetime(2026, 11, 16, 10, tzinfo=ET)
    assert await svc.check(now) == ["exp24:1"]
    assert await svc.check(now) == []
    assert len(n.sent) == 1 and "expires in" in n.sent[0]
