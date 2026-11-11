import time
from datetime import date, datetime

import pytest

from app import scanner as sc
from app.automation import AutomationService
from app.main import OrderIn
from app.notify import ET
from app.options import option_symbol
from app.orders import Ticket, TicketLeg, apply_quotes, prices
from app.sim import SimBroker
from app.store import MemoryStore
from app.strategies import Rules

NOW = datetime(2026, 11, 11, 10, 30, tzinfo=ET)
EXP = date(2026, 12, 18)  # 37 DTE, monthly
WEEKLY = date(2026, 12, 24)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def contract(sym, pc, k, delta, mid, oi=4000):
    return {
        "symbol": sym,
        "bid": round(mid - 0.02, 2),
        "ask": round(mid + 0.02, 2),
        "mark": mid,
        "delta": delta if pc == "C" else -delta,
        "openInterest": oi,
        "volatility": 18.0,
    }


# Strike: (put delta, put mid, call delta, call mid)
BOOK = {
    560: (0.10, 1.20, 0.92, 31.0),
    565: (0.13, 1.60, 0.90, 26.5),
    570: (0.16, 2.10, 0.86, 22.0),
    590: (0.50, 7.00, 0.50, 7.00),
    610: (0.85, 21.0, 0.16, 2.00),
    615: (0.89, 25.5, 0.13, 1.50),
    620: (0.92, 30.0, 0.10, 1.10),
}


def chain(exp=EXP, oi=4000):
    key = f"{exp.isoformat()}:{(exp - NOW.date()).days}"
    puts, calls = {}, {}
    for k, (pd, pm, cd, cm) in BOOK.items():
        puts[f"{k:.1f}"] = [contract(option_symbol("SPY", exp, "P", k), "P", k, pd, pm, oi)]
        calls[f"{k:.1f}"] = [contract(option_symbol("SPY", exp, "C", k), "C", k, cd, cm, oi)]
    return {"underlyingPrice": 590.0, "putExpDateMap": {key: puts}, "callExpDateMap": {key: calls}}


def rows(exp=EXP):
    from app import chain as chain_mod

    return chain_mod.chain_rows(chain(exp))["rows"]


def test_strangle_and_iron_condor_are_built_the_tasty_way():
    s = sc.merged({})
    legs, label = sc.build_trade(rows(), True, s)
    assert label == "Strangle 570/610" and [x["quantity"] for x in legs] == [-1, -1]
    legs, label = sc.build_trade(rows(), False, s)
    # 5 wide: 2.10-1.60 + 2.00-1.50 = 1.00 credit, 20% of the width; 10 wide: 1.80, 18%.
    # Neither reaches a third.
    assert legs is None and "best 20%" in label
    legs, label = sc.build_trade(rows(), False, {**s, "min_ic_credit": 0.18})
    assert label == "Iron condor 565/570/610/615"  # the narrowest wings that collect enough
    assert [x["quantity"] for x in legs] == [1, -1, -1, 1]


def test_expiration_pick_and_form_checks():
    exps = [
        {"date": "2026-12-11", "dte": 30, "monthly": False},
        {"date": "2026-12-18", "dte": 37, "monthly": True},
        {"date": "2027-01-15", "dte": 65, "monthly": True},
    ]
    s = sc.merged({})
    assert sc.pick_expiration(exps, s)["date"] == "2026-12-18"
    assert sc.pick_expiration(exps, {**s, "max_dte": 33}) is None
    assert sc.pick_expiration(exps, {**s, "max_dte": 33, "monthly_only": False})["dte"] == 30
    out = sc.parse_form(
        {"min_ivr": "35", "min_ic_credit": "30", "_form": "scanner", "monthly_only": "on"}, None
    )
    assert out["min_ivr"] == 35 and out["min_ic_credit"] == 0.30 and not out["automanage_new"]
    with pytest.raises(ValueError, match="DTE range"):
        sc.parse_form({"min_dte": "50"}, None)


class Source:
    def __init__(self, kinds):
        self.kinds = kinds

    async def quotes(self, symbols):
        return {s: {"assetMainType": "EQUITY", "assetSubType": self.kinds.get(s)} for s in symbols}

    async def expirations(self, symbol):
        return {
            "expirationList": [
                {"expirationDate": d.isoformat(), "expirationType": t}
                for d, t in ((EXP, "M"), (WEEKLY, "W"))
            ]
        }

    async def option_chain(self, symbol, start, end, strike_count=None):
        return chain(start)


class Portfolio:
    def __init__(self, ivr=60.0):
        self.snapshot = {
            "session": "open",
            "data": {"quotes_at": time.time()},
            "ivr": {"SPY": {"rank": ivr}},
            "account": {"net_liq": 100_000, "bp_ceiling_pct": 0.30},
            "underlyings": [],
        }


class Watchlist:
    async def rows(self):
        return [{"symbol": "AAPL", "ivr": {"rank": 70}}, {"symbol": "XLE", "ivr": {"rank": 10}}]


class LiveBroker:
    async def config(self):
        return {"symbols": ["SPY"], "max_contracts": 1}


class Prefs:
    async def watchlist(self):
        return ["AAPL", "XLE"]

    async def rules(self):
        return Rules()


def quotes_for(body_legs):
    q = {"SPY": {"quote": {"mark": 590.0}}}
    for x in rows():
        for side in ("put", "call"):
            c = x[side]
            q[c["symbol"]] = {
                "quote": {"bidPrice": c["bid"], "askPrice": c["ask"], "mark": c["mark"]}
            }
    return q


def harness(settings=None, ivr=60.0):
    store = MemoryStore()
    if settings:
        store.settings[sc.SETTINGS_KEY] = settings
    broker = SimBroker(store, 100_000, clock=lambda: NOW)
    quotes = quotes_for(None)

    async def submit(raw, place):
        body = OrderIn(**raw)
        t = Ticket(
            legs=[TicketLeg.parse(x.symbol, x.quantity, x.effect) for x in body.legs],
            intent=body.intent,
            duration=body.duration,
            limit_net=body.limit_net,
            profit_target=body.profit_target,
        )
        apply_quotes(t, quotes)
        p = prices(t)
        if not place:
            return {"errors": [], "prices": p, "bp_effect": 4000.0, "bp_used_after": 0.12}
        return await broker.place(t, quotes)

    portfolio = Portfolio(ivr)
    auto = AutomationService(
        store, portfolio, broker, None, Prefs(), None, submit, clock=lambda: NOW
    )
    live_auto = AutomationService(
        store,
        portfolio,
        broker,
        None,
        Prefs(),
        None,
        submit,
        clock=lambda: NOW,
        account="live",
        max_level="propose",
    )
    scanner = sc.Scanner(
        store,
        Source({"SPY": "ETF", "XLE": "ETF"}),
        {"sim": portfolio, "live": portfolio},
        {"sim": auto, "live": live_auto},
        Watchlist(),
        Prefs(),
        LiveBroker(),
        clock=lambda: NOW,
    )
    return scanner, auto, broker


@pytest.mark.anyio
async def test_scan_proposes_a_strangle_and_says_why_others_were_skipped():
    scanner, auto, broker = harness()
    r = await scanner.scan("sim")
    assert [c["symbol"] for c in r["candidates"]] == ["SPY"]
    assert r["candidates"][0]["strategy"] == "Strangle" and r["acted"] == ["propose SPY"]
    reasons = {x["symbol"]: x["reason"] for x in r["skipped"]}
    assert "earnings" in reasons["AAPL"] and "IV rank 10" in reasons["XLE"]
    (p,) = (await auto.view())["proposals"]
    assert p["rule"] == "scan" and p["automanage"] is True
    # Scanning again the same day does not propose it twice.
    assert (await scanner.scan("sim"))["acted"] == []
    # Accepting places it; the fill switches automanage on for the new trade.
    await auto.accept(p["id"])
    order = broker.state["orders"][0]
    assert order["intent"] == "open" and order["status"] == "FILLED"
    await auto.tick()
    assert auto.state["trades"][str(order["trade_id"])] == {"enabled": True}


@pytest.mark.anyio
async def test_low_ivr_buys_defined_risk_and_auto_places_on_sim():
    scanner, auto, broker = harness(
        {"level_sim": "auto", "min_ic_credit": 0.18, "min_ivr": 30}, ivr=40.0
    )
    r = await scanner.scan("sim")
    assert r["candidates"][0]["strategy"] == "Iron condor" and r["acted"] == ["place SPY"]
    assert len(broker.state["orders"][0]["legs"]) == 4


@pytest.mark.anyio
async def test_live_stops_at_propose_and_daily_limit_holds():
    scanner, _, _ = harness({"level_live": "auto", "max_new_per_day": 0})
    view = await scanner.view("live")
    assert view["level"] == "propose"
    r = await scanner.scan("live")
    assert (
        r["acted"] == [] and r["candidates"][0].get("note") == "daily limit of new trades reached"
    )


@pytest.mark.anyio
async def test_daily_scan_runs_once_after_the_scan_time():
    scanner, _, _ = harness()
    assert await scanner.tick() == ["sim"]
    assert await scanner.tick() == []
    early = harness()[0]
    early.clock = lambda: datetime(2026, 11, 11, 9, 50, tzinfo=ET)
    assert await early.tick() == []


class Earnings:
    def __init__(self, dates):
        self.dates = dates

    def is_fund(self, symbol):
        return False

    def info(self, symbol):
        from app.earnings import describe

        d = self.dates.get(symbol)
        return describe({"date": d, "time": "amc"}, NOW.date()) if d else None


@pytest.mark.anyio
async def test_single_stocks_open_when_earnings_are_known_and_far_enough():
    scanner, _, _ = harness()
    scanner.earnings = Earnings({"AAPL": "2027-01-28"})
    r = await scanner.scan("sim", act=False)
    assert "AAPL" in [c["symbol"] for c in r["candidates"]]
    scanner.earnings = Earnings({"AAPL": "2026-11-17"})  # 4 sessions away, buffer 7
    r = await scanner.scan("sim", act=False)
    reasons = {x["symbol"]: x["reason"] for x in r["skipped"]}
    assert "within the 7-session buffer" in reasons["AAPL"]
    scanner.earnings = Earnings({})
    r = await scanner.scan("sim", act=False)
    assert "no earnings date found" in {x["symbol"]: x["reason"] for x in r["skipped"]}["AAPL"]


class History:
    def __init__(self, values):
        self.values = values

    async def ensure(self, symbol):
        start = date(2025, 11, 1)
        from datetime import timedelta

        return [(start + timedelta(days=i), v) for i, v in enumerate(self.values)]


class QuoteSource(Source):
    def __init__(self, kinds, price, change):
        super().__init__(kinds)
        self.price, self.change = price, change

    async def quotes(self, symbols):
        out = await super().quotes(symbols)
        for s in out.values():
            s["quote"] = {"mark": self.price, "netPercentChange": self.change}
        return out


@pytest.mark.anyio
async def test_a_price_extreme_tilts_the_trade_and_ranks_first():
    scanner, _, _ = harness()
    year = [600 - i * 0.05 for i in range(250)]  # drifting down to about 588
    scanner.history = History(year)
    scanner.source = QuoteSource({"SPY": "ETF", "XLE": "ETF"}, 585.0, -2.5)
    r = await scanner.scan("sim", act=False)
    (c,) = r["candidates"]
    assert c["tilt"] == "down" and "tilted bullish" in c["label"]
    # Tilting switched off: neutral as before.
    scanner.store.settings[sc.SETTINGS_KEY] = {"extreme_tilt": False}
    (c,) = (await scanner.scan("sim", act=False))["candidates"]
    assert c["tilt"] is None and c["label"] == "Strangle 570/610"
