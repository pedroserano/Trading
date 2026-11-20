import asyncio
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
    550: (0.06, 0.70, 0.96, 41.0),
    560: (0.10, 1.20, 0.92, 31.0),
    565: (0.13, 1.60, 0.90, 26.5),
    570: (0.16, 2.10, 0.86, 22.0),
    590: (0.50, 7.00, 0.50, 7.00),
    610: (0.85, 21.0, 0.16, 2.00),
    615: (0.89, 25.5, 0.13, 1.50),
    620: (0.92, 30.0, 0.10, 1.10),
    630: (0.96, 40.0, 0.06, 0.60),
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

    async def row(self, symbol):
        return next((r for r in await self.rows() if r["symbol"] == symbol), None)


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
    # Switched on, and managed with the view the scanner leaned with (symmetric: neutral).
    assert auto.state["trades"][str(order["trade_id"])] == {"enabled": True, "view": "neutral"}


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


class QuietEarnings(Earnings):
    def __init__(self, until):
        super().__init__({})
        self.until = until
        self.asked = []

    def quiet_until(self, symbol, recent, gap):
        self.asked.append((recent, gap))
        return {"last": "2026-10-29", "until": self.until} if recent and gap else None


@pytest.mark.anyio
async def test_a_stock_that_just_reported_opens_only_if_it_expires_before_the_next():
    scanner, _, _ = harness()
    scanner.earnings = QuietEarnings("2027-03-01")
    r = await scanner.scan("sim", act=False)
    assert "AAPL" in [c["symbol"] for c in r["candidates"]]
    assert scanner.earnings.asked[0] == (80, 80)  # the defaults
    scanner.earnings = QuietEarnings("2026-12-01")  # before any 45 DTE expiration
    r = await scanner.scan("sim", act=False)
    reason = {x["symbol"]: x["reason"] for x in r["skipped"]}["AAPL"]
    assert "reported 2026-10-29" in reason and "none assumed before 2026-12-01" in reason
    # Off: back to needing a date.
    scanner.store.settings[sc.SETTINGS_KEY] = {"quiet_recent_days": 0}
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
    # The quiet year and a -2.5% day would also read as a possible shock: off here.
    scanner, _, _ = harness({"shock_day_ratio": 1000})
    year = [600 - i * 0.05 for i in range(250)]  # drifting down to about 588
    scanner.history = History(year)
    scanner.source = QuoteSource({"SPY": "ETF", "XLE": "ETF"}, 585.0, -2.5)
    r = await scanner.scan("sim", act=False)
    (c,) = r["candidates"]
    assert c["tilt"] == "down" and "tilted bullish" in c["label"]
    # Tilting switched off: neutral as before.
    scanner.store.settings[sc.SETTINGS_KEY] = {"extreme_tilt": False, "shock_day_ratio": 1000}
    (c,) = (await scanner.scan("sim", act=False))["candidates"]
    assert c["tilt"] is None and c["label"].startswith("Strangle 570/610 (IV rank")


def test_scanner_map_follows_the_settings():
    from app import scanner as scan_mod

    s = scan_mod.merged(None)
    steps = dict(scan_mod.scan_map(s, ["SPY"], "neutral", Rules())["steps"])
    assert list(steps)[0] == "1. Which accounts, and what it does with a trade"
    worth = steps["3. Is the premium worth selling?"]
    assert any("IV rank of 30 or more" in x for x in worth)
    assert any("strangle at 0.16 delta" in x for x in steps["6. What it sells"])
    assert any(
        "Schwab account: SPY (Schwab limits)." in x for x in steps["2. Which stocks it looks at"]
    )
    assert any("at most 20% in what you could be made to buy" in x for x in steps["7. How big"])
    changed = dict(
        scan_mod.scan_map({**s, "min_ivr": 40, "scan_every": 30}, None, "contrarian")["steps"]
    )
    assert any("IV rank of 40 or more" in x for x in changed["3. Is the premium worth selling?"])
    assert any("contrarian" in x for x in changed["5. Which way it leans (the first that applies)"])
    assert any("every 30 minutes" in x for x in changed["2. Which stocks it looks at"])
    assert any(
        "Schwab account: SPY and your watchlist" in x
        for x in changed["2. Which stocks it looks at"]
    )


def test_realized_vol_and_the_volatility_checks():
    import math
    from datetime import date, timedelta

    from app import scanner as scan_mod

    # Alternating +1% / -1% days: about 1% a day, so about 15.9% a year.
    closes, p = [], 100.0
    for i in range(30):
        p *= 1.01 if i % 2 else 0.99
        closes.append((date(2026, 10, 1) + timedelta(days=i), p))
    hv = scan_mod.realized_vol(closes)
    assert hv == pytest.approx(math.sqrt(252) * 1.0, rel=0.05)
    assert scan_mod.realized_vol(closes[:5]) is None

    s = scan_mod.merged(None)
    check = scan_mod.vol_check
    assert check(45, 25.0, 18.0, s) == (None, False)  # elevated, enough premium, edge
    # A high IV rank on a tiny IV in a calm market.
    assert "tiny range" in check(62, 13.8, 10.0, s)[0]
    # Rich premium at an ordinary IV rank: admitted, defined risk only.
    assert check(20, 55.0, 40.0, s) == (None, True)
    assert "under 30" in check(20, 30.0, 20.0, s)[0]
    # Options pricing less movement than the stock is making.
    assert check(45, 18.0, 21.0, s) == (None, False)  # 0.86x: above the 0.8 floor
    assert "less movement" in check(45, 16.0, 21.0, s)[0]
    # Missing data never blocks; each check can be switched off.
    assert check(45, None, None, s) == (None, False)
    off = {**s, "min_iv30": 0, "min_iv_hv": 0, "rich_iv30": 0}
    assert check(62, 10.0, 30.0, off) == (None, False)
    assert "under 30" in check(20, 99.0, 10.0, off)[0]


@pytest.mark.anyio
async def test_extreme_vix_means_defined_risk_only():
    # The test chain's iron condors collect about a fifth of their width.
    scanner, _, _ = harness({"min_ic_credit": 0.15})
    portfolio = scanner.portfolios["sim"]
    portfolio.snapshot["market"] = {"vix": 35.0}
    (c,) = (await scanner.scan("sim", act=False))["candidates"]
    assert c["strategy"] == "Iron condor" and "VIX 35: defined risk only" in c["label"]
    portfolio.snapshot["market"] = {"vix": 20.0}
    (c,) = (await scanner.scan("sim", act=False))["candidates"]
    assert c["strategy"] == "Strangle"
    off, _, _ = harness({"defined_vix": 0})
    portfolio = off.portfolios["sim"]
    portfolio.snapshot["market"] = {"vix": 50.0}
    (c,) = (await off.scan("sim", act=False))["candidates"]
    assert c["strategy"] == "Strangle"


@pytest.mark.anyio
async def test_trades_are_spread_across_sectors():
    scanner, _, _ = harness({"max_per_sector": 2})
    portfolio = scanner.portfolios["sim"]
    # Two broad-index trades open already (QQQ and IWM): SPY would be a third.
    portfolio.snapshot["underlyings"] = [
        {"symbol": "QQQ", "strategies": [{"trade_id": 1, "numbers": {}}]},
        {"symbol": "IWM", "strategies": [{"trade_id": 2, "numbers": {}}]},
    ]
    r = await scanner.scan("sim", act=False)
    reasons = {x["symbol"]: x["reason"] for x in r["skipped"]}
    assert "already 2 open trades in Broad index" in reasons["SPY"]
    # Within one run: SPY and AAPL (here also broad index) with room for one more.
    scanner2, _, _ = harness({"max_per_sector": 1, "max_new_per_day": 5})
    scanner2.earnings = Earnings({"AAPL": "2027-01-28"})
    scanner2.earnings.industries = {"AAPL": "Broad index"}
    r = await scanner2.scan("sim")
    assert len(r["acted"]) == 1
    assert any(
        "would go over 1 trades per sector" in (c.get("note") or "") for c in r["candidates"]
    )
    # A stock of unknown sector is its own sector.
    assert scanner2._sector("ZZZZ") != scanner2._sector("YYYY")


@pytest.mark.anyio
async def test_a_bullish_bias_leans_new_trades_inside_the_band():
    scanner, _, _ = harness()
    snap = scanner.portfolios["sim"].snapshot
    snap["bands"] = {
        "bias": "bullish",
        "delta": {"value": 0.02, "min": 0.0, "max": 0.15, "state": "inside"},
        "theta": {},
    }
    (c,) = (await scanner.scan("sim", act=False))["candidates"]
    assert c["tilt"] == "down" and c["band_lean"]
    assert "tilted bullish: your bullish bias" in c["label"]
    snap["bands"]["bias"] = "neutral"
    snap["bands"]["delta"] = {"value": 0.02, "min": -0.05, "max": 0.05, "state": "inside"}
    (c,) = (await scanner.scan("sim", act=False))["candidates"]
    assert c["tilt"] is None


def _extreme_drop(scanner):
    async def ext(sym, quotes, s):
        return {"direction": "down", "reasons": ["5-session move -2.6σ"]}

    scanner._extreme = ext


@pytest.mark.anyio
async def test_contrarian_bias_sells_against_price_extremes():
    scanner, _, _ = harness({"extreme_tilt": False})
    _extreme_drop(scanner)
    snap = scanner.portfolios["sim"].snapshot
    inside = {"value": 0.0, "min": -0.05, "max": 0.05, "state": "inside"}
    snap["bands"] = {"bias": "neutral", "delta": inside, "theta": {}}
    (c,) = (await scanner.scan("sim", act=False))["candidates"]
    assert c["tilt"] is None  # neutral, extreme lean off: symmetric
    snap["bands"]["bias"] = "contrarian"
    (c,) = (await scanner.scan("sim", act=False))["candidates"]
    assert c["tilt"] == "down" and c["view"] == "bullish"
    assert "tilted bullish: against the move" in c["label"]
    # Outside the band, the book comes first.
    snap["bands"]["delta"] = {"value": 0.09, "min": -0.05, "max": 0.05, "state": "above"}
    (c,) = (await scanner.scan("sim", act=False))["candidates"]
    assert c["tilt"] == "up" and c["lean_source"] == "band"


@pytest.mark.anyio
async def test_a_report_inside_the_trade_can_mean_defined_risk_and_half_size():
    scanner, _, _ = harness(
        {"earnings_inside": "defined", "min_ic_credit": 0.15, "max_bp_share": 0.1}
    )
    scanner.earnings = Earnings({"AAPL": "2026-12-03"})  # before the Dec 18 expiration
    r = await scanner.scan("sim", act=False)
    c = next(x for x in r["candidates"] if x["symbol"] == "AAPL")
    assert c["strategy"] == "Iron condor" and "Defined risk, half size: earnings" in c["label"]
    skip, _, _ = harness()
    skip.earnings = Earnings({"AAPL": "2026-12-03"})
    r = await skip.scan("sim", act=False)
    assert "AAPL" not in [x["symbol"] for x in r["candidates"]]


@pytest.mark.anyio
async def test_the_drop_signal_sells_puts_outside_the_move_and_ranks_first():
    scanner, _, _ = harness({"shock_day_ratio": 1000})
    _extreme_drop(scanner)
    import app.scanner as scmod

    scmod_skew = scmod.skew
    scmod.skew = lambda rows: 9.0
    try:
        (c,) = (await scanner.scan("sim", act=False))["candidates"]
    finally:
        scmod.skew = scmod_skew
    assert c["drop_signal"] and c["label"].startswith("Drop signal (IV rank 60, put skew 9.0")
    assert c["strategy"] == "Short put" and "outside the" in c["label"]


@pytest.mark.anyio
async def test_over_the_notional_limit_the_scanner_opens_a_spread_instead():
    scanner, auto, _ = harness({"min_ic_credit": 0.15})
    first = auto.submit

    async def submit(raw, place):
        out = await first(raw, place)
        naked = all(x["quantity"] < 0 for x in raw["legs"])
        if not place and naked:
            out["errors"] = ["... above the 20% notional limit; use a defined-risk spread"]
        return out

    auto.submit = submit
    (c,) = (await scanner.scan("sim", act=False))["candidates"]
    assert c["strategy"] == "Iron condor"


@pytest.mark.anyio
async def test_a_proposed_find_leaves_the_list_once_placed_or_dismissed():
    scanner, auto, _ = harness()
    await scanner.scan("sim")
    (c,) = (await scanner.view("sim"))["last"]["candidates"]
    (p,) = (await auto.view())["proposals"]
    # The find carries its proposal, the order to review, and its reasons apart.
    assert c["proposal_id"] == p["id"] and c["body"]["legs"]
    assert c["title"].startswith("Strangle") and c["detail"] and c["title"] not in c["detail"]
    # Placed from the ticket on Positions: off the list here too, and not proposed again.
    await auto.placed_from_ticket(p["id"])
    assert (await scanner.view("sim"))["last"]["candidates"] == []
    assert (await auto.view())["proposals"] == []


@pytest.mark.anyio
async def test_dismissing_a_find_dismisses_its_proposal_and_survives_a_rescan():
    scanner, auto, _ = harness()
    await scanner.scan("sim")
    await scanner.dismiss("sim", "SPY")
    assert (await scanner.view("sim"))["last"]["candidates"] == []
    assert (await auto.view())["proposals"] == []
    await scanner.scan("sim")
    assert (await scanner.view("sim"))["last"]["candidates"] == []


@pytest.mark.anyio
async def test_a_low_float_means_defined_risk_at_half_size():
    scanner, _, _ = harness({"min_ic_credit": 0.15, "max_bp_share": 0.1})
    scanner.earnings = Earnings({"AAPL": "2027-01-28"})
    asked = []

    def float_cached(sym):  # the float as the watchlist last read it: never fetched
        asked.append(sym)
        return {"shares": 7.2e6, "low": True} if sym == "AAPL" else None

    r = await scanner.scan("sim", act=False)
    c = next(x for x in r["candidates"] if x["symbol"] == "AAPL")
    assert c["strategy"] == "Strangle"
    scanner.watchlist.float_cached = float_cached
    r = await scanner.scan("sim", act=False)
    c = next(x for x in r["candidates"] if x["symbol"] == "AAPL")
    assert c["strategy"] == "Iron condor"
    assert "Defined risk, half size: low float (7.2 million shares)" in c["label"]
    # Funds have no float to check.
    assert "XLE" not in asked and "SPY" not in asked


# Earnings play and post-earnings fade ------------------------------------------------


class EarningsAt(Earnings):
    """Reports with a time, and the last report already past."""

    def __init__(self, dates, time_="amc", last=None):
        super().__init__(dates)
        self.time_, self.last = time_, last or {}

    def info(self, symbol):
        from app.earnings import describe

        d = self.dates.get(symbol)
        return describe({"date": d, "time": self.time_}, NOW.date()) if d else None

    def last_report(self, symbol):
        return self.last.get(symbol)


@pytest.mark.anyio
async def test_earnings_play_is_off_by_default_then_sells_into_the_report():
    scanner, _, _ = harness({"min_ic_credit": 0.15})
    scanner.earnings = EarningsAt({"AAPL": "2026-11-11"})  # after today's close
    r = await scanner.scan("sim", act=False)
    assert "AAPL" not in [c["symbol"] for c in r["candidates"]]
    assert "buffer" in {x["symbol"]: x["reason"] for x in r["skipped"]}["AAPL"]

    # A play may take a quarter of the per-trade buying power: 4,000 is over 2.5%.
    scanner.store.settings[sc.SETTINGS_KEY] = {
        "min_ic_credit": 0.15,
        "max_bp_share": 0.1,
        "earnings_play": True,
    }
    r = await scanner.scan("sim", act=False)
    assert "over 2.5% of net liq" in {x["symbol"]: x["reason"] for x in r["skipped"]}["AAPL"]
    scanner.store.settings[sc.SETTINGS_KEY] = {
        "min_ic_credit": 0.15,
        "max_bp_share": 0.2,
        "earnings_play": True,
    }
    r = await scanner.scan("sim", act=False)
    c = next(x for x in r["candidates"] if x["symbol"] == "AAPL")
    assert "Quarter size, spare capital only" in c["label"]
    # Spare capital only: within 5 points of the VIX ceiling (12% after, ceiling 15%).
    scanner.portfolios["sim"].snapshot["account"]["bp_ceiling_pct"] = 0.15
    r = await scanner.scan("sim", act=False)
    assert "spare capital only" in {x["symbol"]: x["reason"] for x in r["skipped"]}["AAPL"]
    scanner.portfolios["sim"].snapshot["account"]["bp_ceiling_pct"] = 0.30
    assert c["play"] == "earnings:2026-11-12" and c["strategy"] == "Iron condor"
    assert c["label"].startswith("Earnings play: ")
    assert "closed on 2026-11-12 after the IV crush" in c["label"]
    # The move options price for the report is remembered for the fade.
    assert (await scanner._state())["pre_em"]["AAPL"]["report"] == "2026-11-11"

    scanner.store.settings[sc.SETTINGS_KEY] = {
        "min_ic_credit": 0.15,
        "max_bp_share": 0.2,
        "earnings_play": True,
        "earnings_play_risk": "undefined",
        # The test chain's strikes stop at 550/630: half the ±33.81 expected move away.
        "earnings_play_em": 0.5,
    }
    c = next(
        x for x in (await scanner.scan("sim", act=False))["candidates"] if x["symbol"] == "AAPL"
    )
    assert c["strategy"] == "Strangle"
    assert c["title"] == "Strangle 570/610 (0.5× the ±33.81 expected move)"
    # IV rank under the play's floor, or no report time: skipped, saying why.
    scanner.store.settings[sc.SETTINGS_KEY] = {"earnings_play": True, "earnings_play_ivr": 80}
    r = await scanner.scan("sim", act=False)
    assert (
        "earnings play: IV rank 70, under 80"
        in {x["symbol"]: x["reason"] for x in r["skipped"]}["AAPL"]
    )
    scanner.earnings = EarningsAt({"AAPL": "2026-11-11"}, time_=None)
    scanner.store.settings[sc.SETTINGS_KEY] = {"earnings_play": True}
    r = await scanner.scan("sim", act=False)
    assert "time" in {x["symbol"]: x["reason"] for x in r["skipped"]}["AAPL"]


@pytest.mark.anyio
async def test_a_proposed_earnings_play_carries_its_tag():
    scanner, auto, _ = harness(
        {"min_ic_credit": 0.15, "max_bp_share": 0.2, "earnings_play": True, "level": "propose"}
    )
    scanner.earnings = EarningsAt({"AAPL": "2026-11-11"})
    await scanner.scan("sim", act=True)
    p = next(x for x in (await auto.load())["proposals"] if x["underlying"] == "AAPL")
    assert p["play"] == "earnings:2026-11-12"


def test_fade_signal():
    s = sc.merged({"fade": True})
    flat = [(date(2026, 10, 1 + i), 100.0 + (i % 2) * 0.5) for i in range(25)]
    today = date(2026, 11, 11)
    # Down 8% since the close before the report, against a recorded 5% expected move.
    f = sc.fade_signal(flat, 92.46, "2026-11-10", today, s, {"report": "2026-11-10", "em": 0.05})
    assert f["direction"] == "down" and f["basis"] == "its 5.0% expected move"
    # Inside the expected move: no fade.
    assert (
        sc.fade_signal(flat, 97.0, "2026-11-10", today, s, {"report": "2026-11-10", "em": 0.05})
        is None
    )
    # No move recorded: against 3x the usual day (about 0.5%).
    f = sc.fade_signal(flat, 103.0, "2026-11-10", today, s, None)
    assert f["direction"] == "up" and "usual" in f["basis"]
    # Too long after the report, or on the report day itself.
    assert sc.fade_signal(flat, 92.0, "2026-11-03", today, s, None) is None
    assert sc.fade_signal(flat, 92.0, "2026-11-11", today, s, None) is None


@pytest.mark.anyio
async def test_post_earnings_fade_sells_against_the_move_when_on():
    class Priced(Source):
        async def quotes(self, symbols):
            out = await super().quotes(symbols)
            out["AAPL"]["quote"] = {"mark": 560.0, "netPercentChange": -1.0}
            return out

    def setup(settings):
        scanner, _, _ = harness(settings)
        scanner.source = Priced({"SPY": "ETF", "XLE": "ETF"})
        scanner.history = History([600.0 + (i % 2) for i in range(30)])
        scanner.earnings = EarningsAt({"AAPL": "2027-02-01"}, last={"AAPL": "2026-11-10"})
        return scanner

    off = setup({"min_ic_credit": 0.15, "extreme_tilt": False, "shock_day_ratio": 1000})
    c = next(x for x in (await off.scan("sim", act=False))["candidates"] if x["symbol"] == "AAPL")
    assert c["fade"] is None and c["strategy"] == "Strangle"

    on = setup(
        {"min_ic_credit": 0.15, "extreme_tilt": False, "shock_day_ratio": 1000, "fade": True}
    )
    c = next(x for x in (await on.scan("sim", act=False))["candidates"] if x["symbol"] == "AAPL")
    assert c["fade"]["direction"] == "down" and c["strategy"] == "Short put"
    assert c["label"].startswith("Post-earnings fade (down 6.8% since the 2026-11-10 report")
    assert "against the post-earnings move" in c["label"]


def test_an_earnings_strangle_sits_twice_the_expected_move_away_by_default():
    s = sc.merged({})
    assert s["earnings_play_em"] == 2.0
    # Spot 590, expected move 10: strikes at or beyond 570 and 610.
    legs, label = sc.build_trade(rows(), True, s, None, (590.0, 10.0, s["earnings_play_em"]))
    assert label == "Strangle 570/610 (2× the ±10.00 expected move)"
    assert [x["quantity"] for x in legs] == [-1, -1]
    legs, label = sc.build_trade(rows(), True, s, None, (590.0, 30.0, 2.0))
    assert legs is None and "no strikes 2× the ±30.00 expected move away" in label


@pytest.mark.anyio
async def test_a_post_earnings_fade_is_never_proposed_under_iv_rank_20():
    class Priced(Source):
        async def quotes(self, symbols):
            out = await super().quotes(symbols)
            out["AAPL"]["quote"] = {"mark": 560.0, "netPercentChange": -1.0}
            return out

    class LowIv(Watchlist):
        async def rows(self):
            return [{"symbol": "AAPL", "ivr": {"rank": 15}}, {"symbol": "XLE", "ivr": {"rank": 10}}]

    scanner, _, _ = harness(
        {
            "min_ic_credit": 0.15,
            "extreme_tilt": False,
            "shock_day_ratio": 1000,
            "fade": True,
            "rich_iv30": 1,  # rich enough at any IV rank: still no fade under 20
        }
    )
    scanner.source = Priced({"SPY": "ETF", "XLE": "ETF"})
    scanner.watchlist = LowIv()
    scanner.history = History([600.0 + (i % 2) for i in range(30)])
    scanner.earnings = EarningsAt({"AAPL": "2027-02-01"}, last={"AAPL": "2026-11-10"})
    r = await scanner.scan("sim", act=False)
    assert "AAPL" not in [c["symbol"] for c in r["candidates"]]
    assert "IV rank 15, under 20" in {x["symbol"]: x["reason"] for x in r["skipped"]}["AAPL"]
    assert sc.merged({})["fade_min_ivr"] == 20
    with pytest.raises(ValueError, match="between 20"):
        sc.parse_form({"fade_min_ivr": "10"}, None)


@pytest.mark.anyio
async def test_a_scan_by_hand_runs_in_the_background_with_progress():
    scanner, _, _ = harness()
    release = asyncio.Event()
    real = scanner._check

    async def slow(*a, **k):
        await release.wait()
        return await real(*a, **k)

    scanner._check = slow
    scanner.start("sim", act=False)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    v = await scanner.view("sim")
    assert v["running"]["total"] >= 1 and v["running"]["done"] == 0
    scanner.start("sim", act=False)  # a second press while running starts nothing new
    release.set()
    await scanner.tasks["sim"]
    v = await scanner.view("sim")
    assert v["running"] is None and v["error"] is None and v["last"] is not None

    async def broken(*a, **k):
        raise RuntimeError("Schwab said no")

    scanner._check = broken
    scanner.start("sim", act=False)
    await scanner.tasks["sim"]
    assert "Schwab said no" in (await scanner.view("sim"))["error"]


@pytest.mark.anyio
async def test_the_scan_reads_stored_price_history_without_asking_schwab():
    from app.extremes import PriceHistory
    from app.store import MemoryStore

    class NoSchwab:
        async def daily_closes(self, symbol, days):
            raise AssertionError("the scan must not fetch price history")

    store = MemoryStore()
    today = datetime(2026, 11, 19, 12, tzinfo=sc.ET)
    await store.upsert_prices("AAPL", [(date(2026, 11, 17), 230.0), (date(2026, 11, 18), 231.0)])
    h = PriceHistory(store, NoSchwab(), None, clock=lambda: today)
    assert await h.cached("AAPL") == [(date(2026, 11, 17), 230.0), (date(2026, 11, 18), 231.0)]
    assert await h.cached("MSFT") is None


@pytest.mark.anyio
async def test_the_scan_uses_what_the_app_already_holds():
    """Quotes, expirations, liquidity and float from the app's own reads; Schwab is asked only
    for the full chain of a symbol that passed everything else."""
    scanner, _, _ = harness()
    scanner.earnings = Earnings({"AAPL": "2027-01-28"})
    calls = []
    src = scanner.source

    class Counting:
        async def quotes(self, symbols):
            calls.append(("quotes", tuple(symbols)))
            return await src.quotes(symbols)

        async def expirations(self, symbol):
            calls.append(("expirations", symbol))
            return await src.expirations(symbol)

        async def option_chain(self, symbol, *a, **k):
            calls.append(("chain", symbol))
            return await src.option_chain(symbol, *a, **k)

    scanner.source = Counting()
    held = await src.quotes(["SPY", "AAPL", "XLE"])
    for q in held.values():
        q["quote"] = {"mark": 100.0}
    scanner.portfolios["sim"].quotes = held
    at = NOW.timestamp()
    scanner.watchlist.cache = {
        sym: {"at": at, "exp_dates": [EXP.isoformat(), WEEKLY.isoformat()]}
        for sym in ("SPY", "AAPL", "XLE")
    }
    # AAPL too thin by the watchlist's own read of the same expiration: never fetched.
    scanner.watchlist.cache["AAPL"].update(
        {"expiration": EXP.isoformat(), "spread_pct": 0.2, "atm_oi": 5000}
    )
    r = await scanner.scan("sim", act=False)
    reasons = {x["symbol"]: x["reason"] for x in r["skipped"]}
    assert "spread 20.0% is too wide" in reasons["AAPL"]
    # Only SPY, which passed every local check, needed Schwab: its full chain.
    assert calls == [("chain", "SPY")]


def test_presets_set_how_boldly_it_sells_and_leave_the_rest():
    from app import scanner as scan_mod

    s = scan_mod.merged({})
    assert scan_mod.preset_of(s) == "standard"
    saved = {"contracts": 2, "scan_time": "11:00"}
    c = scan_mod.merged(scan_mod.apply_preset(saved, "conservative"))
    assert scan_mod.preset_of(c) == "conservative"
    assert c["min_ivr"] == 40 and c["contracts"] == 2 and c["scan_time"] == "11:00"
    e = scan_mod.merged(scan_mod.apply_preset(c, "earnings"))
    assert e["earnings_play"] and e["fade"] and e["min_ivr"] == 30
    assert scan_mod.preset_of({**e, "min_ivr": 35}) is None  # Custom
