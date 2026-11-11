import asyncio
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from app import chain
from app.analysis import TradeLeg, analyze
from app.config import Settings
from app.datasource import DemoDataSource
from app.main import create_app
from app.notify import ET
from app.portfolio import PortfolioService
from app.preferences import Preferences, parse_symbols
from app.store import MemoryStore
from app.streaming import StreamService

TODAY = date(2026, 11, 11)
EXP = date(2026, 12, 18)
PASSWORD = "p" * 12


@pytest.fixture
def anyio_backend():
    return "asyncio"


def settings(**kw):
    return Settings(
        _env_file=None,
        app_password=PASSWORD,
        secret_key="s" * 32,
        demo_mode=True,
        cookie_secure=False,
        **kw,
    )


# Watchlist import ------------------------------------------------------------------------

TOS_EXPORT = """\
"Watchlist: Premium sellers"

Symbol,Last,Net Chng,IV Percentile,
SPY,590.12,+1.05,24%,
QQQ,510.33,-0.20,31%,
/ES,5920.25,+4.50,,
.SPY261218P550,4.85,,,
$SPX,5901.10,+10.2,,
BRK/B,470.1,,,
spy,590.12,,,
"""


def test_parse_thinkorswim_export():
    symbols, skipped = parse_symbols(TOS_EXPORT)
    assert symbols == ["SPY", "QQQ", "$SPX", "BRK/B"]
    assert skipped == ["/ES", ".SPY261218P550"]


def test_parse_pasted_list():
    assert parse_symbols("spy, qqq iwm\nGLD;tlt,,") == (["SPY", "QQQ", "IWM", "GLD", "TLT"], [])


@pytest.mark.anyio
async def test_rules_validation_and_defaults():
    prefs = Preferences(MemoryStore(), settings(watchlist="SPY,IWM"))
    assert await prefs.watchlist() == ["SPY", "IWM"]
    r = await prefs.set_rules(
        {
            "profit_target": "40",
            "manage_dte": "14",
            "tested_delta": "0.35",
            "loss_multiple": "3",
            "profit_target_straddle": "25",
        }
    )
    assert (r.profit_target, r.manage_dte, r.tested_delta, r.notify_flags) == (0.4, 14, 0.35, False)
    with pytest.raises(ValueError, match="between 5 and 100"):
        await prefs.set_rules({"profit_target": "150"})
    assert (await prefs.rules()).profit_target == 0.4


# Chains ---------------------------------------------------------------------------------


def test_expirations_and_default_is_monthly_near_45():
    raw = {
        "expirationList": [
            {"expirationDate": d}
            for d in (
                "2026-11-13",
                "2026-12-11",
                "2026-12-18",
                "2026-12-24",
                "2027-01-15",
                "2026-11-06",
            )
        ]
    }
    exps = chain.expirations(raw, TODAY)
    assert [e["date"] for e in exps][0] == "2026-11-13"  # past date dropped
    assert {e["date"] for e in exps if e["monthly"]} == {"2026-12-18", "2027-01-15"}
    assert chain.default_expiration(exps) == "2026-12-18"


def test_chain_rows_accept_both_field_spellings():
    raw = {
        "underlyingPrice": 590.0,
        "callExpDateMap": {
            "2026-12-18:37": {
                "590.0": [{"bid": 9.9, "ask": 10.1, "delta": 0.52, "volatility": 15.0}]
            }
        },
        "putExpDateMap": {
            "2026-12-18:37": {
                "590.0": [{"bidPrice": 9.0, "askPrice": 9.4, "delta": -0.48, "volatility": 17.0}]
            }
        },
    }
    out = chain.chain_rows(raw)
    row = out["rows"][0]
    assert row["call"]["mark"] == pytest.approx(10.0) and row["put"]["mark"] == pytest.approx(9.2)
    assert out["atm_iv"] == 16.0 and out["dte"] == 37
    assert out["expected_move"] == pytest.approx(590 * 0.16 * (37 / 365) ** 0.5, abs=0.01)


# Analysis -------------------------------------------------------------------------------


def put_vertical():
    return [
        TradeLeg("P", 575, EXP, -1, 4.00, iv=16, delta=-0.30, theta=-0.10, vega=0.6),
        TradeLeg("P", 570, EXP, 1, 3.00, iv=17, delta=-0.25, theta=-0.09, vega=0.55),
    ]


def test_vertical_analysis():
    a = analyze(put_vertical(), 590, TODAY, vol=15)
    assert a["net"] == 100 and a["credit"] and a["price_per_unit"] == 1.0
    assert a["max_profit"] == 100 and a["max_loss"] == 400
    assert a["breakevens"] == [574.0]
    assert 0.6 < a["pop"] < 0.85
    assert a["delta"] == pytest.approx(5.0) and a["theta"] == pytest.approx(1.0)
    assert a["bp_estimate"] == 400 and a["bp_exact"]
    assert len(a["curve"]["x"]) == len(a["curve"]["expiry"]) == len(a["curve"]["today"])
    assert 575 in a["curve"]["x"] and 570 in a["curve"]["x"]


def test_strangle_analysis_is_unlimited_with_two_breakevens():
    legs = [TradeLeg("P", 555, EXP, -1, 4.85, iv=19), TradeLeg("C", 610, EXP, -1, 3.95, iv=13)]
    a = analyze(legs, 590, TODAY)
    assert a["max_profit"] == 880 and a["unlimited_loss"] and a["max_loss"] is None
    assert a["breakevens"] == [546.2, 618.8]
    assert 0.6 < a["pop"] < 0.9
    # Reg T: the call side (20% of 590, less 20 OTM, plus 3.95) is the larger requirement,
    # plus the put's premium.
    assert a["bp_estimate"] == pytest.approx((0.2 * 590 - 20 + 3.95) * 100 + 485)
    assert not a["bp_exact"]


def test_long_call_unlimited_profit():
    a = analyze([TradeLeg("C", 600, EXP, 2, 5.0, iv=15)], 590, TODAY)
    assert a["unlimited_profit"] and a["max_loss"] == 1000 and a["breakevens"] == [605.0]
    assert not a["credit"]


def test_analysis_rejects_nothing_to_analyse():
    with pytest.raises(ValueError):
        analyze([], 590, TODAY)


# Streaming ------------------------------------------------------------------------------


class FakeStreamClient:
    def __init__(self, messages):
        self.messages = list(messages)
        self.handlers = {}
        self.subs = []

    async def login(self):
        pass

    def add_level_one_option_handler(self, h):
        self.handlers["opt"] = h

    def add_level_one_equity_handler(self, h):
        self.handlers["eq"] = h

    def add_account_activity_handler(self, h):
        self.handlers["acct"] = h

    async def account_activity_sub(self):
        self.subs.append(("acct",))

    async def level_one_equity_subs(self, symbols):
        self.subs.append(("eq", tuple(symbols)))

    async def level_one_option_subs(self, symbols):
        self.subs.append(("opt", tuple(symbols)))

    async def level_one_equity_unsubs(self, symbols):
        self.subs.append(("eq-", tuple(symbols)))

    async def level_one_option_unsubs(self, symbols):
        self.subs.append(("opt-", tuple(symbols)))

    async def handle_message(self):
        if not self.messages:
            await asyncio.sleep(3600)
        kind, msg = self.messages.pop(0)
        self.handlers[kind](msg)


class Connected:
    def status(self):
        class S:
            connected = True

        return S()

    def client(self):
        return object()


@pytest.mark.anyio
async def test_stream_subscribes_merges_quotes_and_refreshes_on_activity():
    opt = "SPY   261218P00555000"
    received, activity = {}, []
    fake = FakeStreamClient(
        [
            ("eq", {"content": [{"key": "SPY", "MARK": 591.2, "NET_CHANGE_PERCENT": 0.3}]}),
            ("opt", {"content": [{"key": opt, "MARK": 4.1, "DELTA": -0.2, "BOGUS": 1}]}),
            ("acct", {"content": [{"key": "x"}]}),
        ]
    )
    svc = StreamService(
        Connected(),
        lambda: {"SPY", "$VIX", opt},
        received.update,
        lambda: activity.append(1),
        client_factory=lambda c: fake,
    )
    task = asyncio.create_task(svc.run())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if activity:
            break
    task.cancel()
    assert svc.status == "live"
    assert ("eq", ("SPY",)) in fake.subs and ("opt", (opt,)) in fake.subs  # $VIX not streamed
    assert received["SPY"] == {"mark": 591.2, "netPercentChange": 0.3}
    assert received[opt] == {"mark": 4.1, "delta": -0.2}
    assert activity == [1] and svc.fresh("SPY") and not svc.fresh("$VIX")


@pytest.mark.anyio
async def test_portfolio_merges_streamed_quotes_and_polls_only_stale_symbols():
    source = DemoDataSource(today=TODAY)
    calls = []
    orig = source.quotes

    async def spy_quotes(symbols):
        calls.append(list(symbols))
        return await orig(symbols)

    source.quotes = spy_quotes

    class Live:
        status, detail = "live", None

        def fresh(self, s, now=None):
            return s != "$VIX"

    p = PortfolioService(
        source,
        MemoryStore(),
        settings(),
        lambda: {},
        _demo_resolver,
        "demo",
        clock=lambda: datetime(2026, 11, 11, 11, tzinfo=ET),
    )
    p.stream = Live()
    await p.tick()  # first poll is always full
    assert len(calls[0]) > 2
    await p.tick()
    assert calls[1] == ["$VIX"]
    p.on_stream_quotes({"SPY": {"mark": 600.0}})
    assert p._price("SPY") == 600.0 and p._dirty
    assert p.snapshot["data"]["source"] == "stream"


async def _demo_resolver():
    return DemoDataSource.HASH


# API ------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    s = settings(data_dir=tmp_path)
    app = create_app(
        s,
        store=MemoryStore(),
        start_background=False,
        static_dir=tmp_path / "none",
        source=DemoDataSource(today=TODAY),
    )
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": PASSWORD})
        yield c


def test_trade_endpoints(client):
    exps = client.get("/api/trade/SPY/expirations").json()
    assert exps["default"] in {e["date"] for e in exps["expirations"]}
    ch = client.get(f"/api/trade/SPY/chain?expiration={exps['default']}&strikes=10").json()
    assert len(ch["rows"]) == 21 and ch["underlying_price"] > 0
    row = next(r for r in ch["rows"] if r["put"]["delta"] and r["put"]["delta"] > -0.35)
    leg = {
        "put_call": "P",
        "strike": row["strike"],
        "expiration": exps["default"],
        "quantity": -1,
        "price": row["put"]["mark"],
        "iv": row["put"]["iv"],
        "delta": row["put"]["delta"],
        "theta": row["put"]["theta"],
    }
    a = client.post(
        "/api/analyze", json={"symbol": "SPY", "spot": ch["underlying_price"], "legs": [leg]}
    ).json()
    assert a["credit"] and a["unlimited_loss"] is False and a["max_loss"] > 0
    assert "portfolio_after" in a and a["beta"] == 1.0
    assert client.get("/api/trade/bad!sym/expirations").status_code == 400
    assert client.get("/api/trade/BRK%2FB/expirations").status_code == 200
    bad = client.post("/api/analyze", json={"symbol": "SPY", "spot": 590, "legs": []})
    assert bad.status_code == 422


def test_watchlist_import_and_remove(client):
    r = client.post(
        "/watchlist/import",
        files={"file": ("wl.csv", TOS_EXPORT, "text/csv")},
        data={"mode": "replace"},
    )
    assert r.status_code == 303
    page = client.get("/connect").text
    assert "Watchlist now has 4 symbols" in page and "Skipped" in page and "/ES" in page
    client.post("/watchlist/import", data={"text": "GLD tlt", "mode": "add"})
    client.post("/watchlist/remove", data={"symbol": "QQQ"})
    page = client.get("/connect").text
    for sym in ("SPY", "GLD", "TLT", "BRK/B"):
        assert f'value="{sym}"' in page
    assert 'value="QQQ"' not in page
    assert client.get("/api/watchlist").status_code == 200


def test_rules_form(client):
    client.post(
        "/rules",
        data={
            "profit_target": "40",
            "profit_target_straddle": "25",
            "manage_dte": "14",
            "tested_delta": "0.3",
            "loss_multiple": "2",
            "notify_flags": "on",
        },
    )
    page = client.get("/connect").text
    assert "Rules saved" in page and 'value="40"' in page and 'value="14"' in page
    client.post("/rules", data={"manage_dte": "999"})
    assert "Manage at DTE: must be between 0 and 60" in client.get("/connect").text


class Notifier:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send(self, title, message, url="", priority=0):
        self.sent.append((title, message))


@pytest.mark.anyio
async def test_flag_notifications_once_each_and_respect_switch():
    store, n = MemoryStore(), Notifier()
    prefs = Preferences(store, settings())
    p = PortfolioService(
        DemoDataSource(today=TODAY),
        store,
        settings(),
        lambda: {},
        _demo_resolver,
        "live",
        clock=lambda: datetime(2026, 11, 11, 11, tzinfo=ET),
        preferences=prefs,
        notifier=n,
    )
    await p.tick()
    titles = [t for t, _ in n.sent]
    assert any(t.startswith("SPY Put vertical") for t in titles)  # inside 21 DTE
    assert any(t.startswith("IWM Short put") for t in titles)  # tested
    count = len(n.sent)
    await p.tick()
    assert len(n.sent) == count
    store.notified.clear()
    await prefs.set_rules({})  # checkbox absent = notifications off
    await p.tick()
    assert len(n.sent) == count
