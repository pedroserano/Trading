import time
from datetime import date, datetime

import pytest

from app import automation as auto
from app.automation import Action, AutomationService, decide, merged_settings, sides
from app.main import OrderIn
from app.notify import ET
from app.options import option_symbol
from app.orders import Ticket, TicketLeg, apply_quotes, prices
from app.sim import SimBroker
from app.store import MemoryStore
from app.strategies import Rules

EXP = date(2026, 12, 18)
NEXT = date(2027, 1, 15)
NOW = datetime(2026, 11, 11, 11, 0, tzinfo=ET)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def sym(pc, k, exp=EXP):
    return option_symbol("SPY", exp, pc, k)


def q(bid, ask):
    return {"quote": {"bidPrice": bid, "askPrice": ask, "mark": round((bid + ask) / 2, 4)}}


def leg(pc, k, n, delta=-0.16, exp=EXP):
    return {
        "symbol": sym(pc, k, exp),
        "kind": "OPTION",
        "put_call": pc,
        "strike": k,
        "expiration": exp.isoformat(),
        "quantity": n,
        "delta": delta,
    }


def strat(
    name="Strangle", dte=37, flags=(), defined=False, rolls=0, legs=None, tid=1, open_value=580.0
):
    legs = legs or [leg("P", 555, -1, -0.16), leg("C", 610, -1, 0.15)]
    return {
        "trade_id": tid,
        "rolls": rolls,
        "name": name,
        "defined_risk": defined,
        "flags": [{"kind": f} for f in flags],
        "legs": legs,
        "numbers": {
            "dte": dte,
            "open_value": open_value,
            "contracts": 1,
            "expiration": EXP.isoformat(),
        },
    }


ALL = {r: "auto" for r in auto.RULES}


def kinds(actions):
    return [(a.rule, a.kind) for a in actions]


# Deciding --------------------------------------------------------------------------------


def test_decide_follows_the_rules():
    s = merged_settings({})
    r = Rules()
    assert kinds(decide(strat(), 590, r, s, ALL)) == [("profit", "target")]
    assert kinds(decide(strat(dte=20), 590, r, s, ALL)) == [("dte", "roll"), ("profit", "target")]
    assert ("dte", "close") in kinds(decide(strat(dte=20, defined=True), 590, r, s, ALL))
    assert ("dte", "close") in kinds(decide(strat(dte=20, rolls=3), 590, r, s, ALL))
    assert ("tested", "roll_untested") in kinds(decide(strat(flags=["tested"]), 590, r, s, ALL))
    assert ("tested", "notify") in kinds(
        decide(strat(name="Iron condor", flags=["tested"], defined=True), 590, r, s, ALL)
    )
    assert kinds(decide(strat(flags=["loss"]), 590, r, s, ALL))[0] == ("loss", "close")
    assert kinds(decide(strat(dte=0), 620, r, s, ALL))[0] == ("expiry", "close")
    assert not any(a.rule == "expiry" for a in decide(strat(dte=0), 580, r, s, ALL))
    # Profit below auto: close only once the target is reached.
    lv = {**ALL, "profit": "propose"}
    assert kinds(decide(strat(), 590, r, s, lv)) == []
    assert kinds(decide(strat(flags=["profit"]), 590, r, s, lv)) == [("profit", "close")]
    # Off means off.
    assert decide(strat(dte=20, flags=["loss"]), 590, r, s, {r_: "off" for r_ in ALL}) == []


def test_iron_condor_rolls_by_side_and_closes_whole():
    ic = strat(
        name="Iron condor",
        defined=True,
        legs=[leg("P", 550, 1), leg("P", 555, -1), leg("C", 610, -1), leg("C", 615, 1)],
    )
    assert [len(x) for x in sides(ic, "close")] == [4]
    assert [{x["put_call"] for x in side} for side in sides(ic, "roll")] == [{"P"}, {"C"}]


def test_target_expiration_prefers_monthlies_near_the_target():
    exps = [
        {"date": "2026-12-11", "dte": 30, "monthly": False},
        {"date": "2026-12-18", "dte": 37, "monthly": True},
        {"date": "2026-12-24", "dte": 43, "monthly": False},
        {"date": "2027-01-15", "dte": 65, "monthly": True},
    ]
    assert auto.target_expiration(exps, "2026-12-01", 45) == "2026-12-18"
    assert auto.target_expiration(exps, "2026-12-18", 45) == "2027-01-15"
    assert auto.target_expiration(exps, "2027-01-15", 45) is None


def test_settings_form_is_validated():
    s = auto.parse_settings(
        {
            "level_dte": "auto",
            "max_concession": "40",
            "daily_cap": "5",
            "window_start": "10:00",
            "window_end": "15:00",
        },
        None,
    )
    assert (s["levels"]["dte"], s["max_concession"], s["daily_cap"]) == ("auto", 0.4, 5)
    with pytest.raises(ValueError, match="between"):
        auto.parse_settings({"daily_cap": "0"}, None)
    with pytest.raises(ValueError, match="window"):
        auto.parse_settings({"window_start": "15:00", "window_end": "10:00"}, None)
    with pytest.raises(ValueError, match="unknown level"):
        auto.parse_settings({"level_dte": "yolo"}, None)


# Running ---------------------------------------------------------------------------------


class Prefs:
    async def rules(self):
        return Rules()


class Notifier:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send(self, title, message, url="", priority=0):
        self.sent.append((title, message))


class Portfolio:
    def __init__(self, strategies, spot=590.0):
        self.snapshot = {
            "session": "open",
            "data": {"quotes_at": time.time()},
            "underlyings": [{"symbol": "SPY", "price": spot, "strategies": strategies}],
        }


class Source:
    async def expirations(self, symbol):
        return {
            "expirationList": [
                {"expirationDate": "2026-12-18", "expirationType": "M"},
                {"expirationDate": "2027-01-15", "expirationType": "M"},
            ]
        }

    async def option_chain(self, symbol, start, end, strike_count=None):
        exp = start
        key = f"{exp.isoformat()}:{(exp - NOW.date()).days}"

        def c(pc, k, d):
            return [{"symbol": sym(pc, k, exp), "bid": 1.0, "ask": 1.1, "delta": d}]

        return {
            "underlyingPrice": 590,
            "putExpDateMap": {key: {"555.0": c("P", 555, -0.16), "570.0": c("P", 570, -0.3)}},
            "callExpDateMap": {key: {"610.0": c("C", 610, 0.15)}},
        }


class Harness:
    """A real simulated broker; quotes for previews and fills can differ to test stepping."""

    def __init__(self, strategies, quotes, fill_quotes=None, settings=None):
        self.store = MemoryStore()
        if settings:
            self.store.settings[auto.SETTINGS_KEY] = settings
        self.broker = SimBroker(self.store, 10_000, clock=lambda: NOW)
        self.quotes = quotes
        self.fill_quotes = fill_quotes or quotes
        self.notifier = Notifier()
        self.portfolio = Portfolio(strategies)
        self.svc = AutomationService(
            self.store,
            self.portfolio,
            self.broker,
            Source(),
            Prefs(),
            self.notifier,
            self.submit,
            clock=lambda: NOW,
        )

    async def submit(self, raw, place):
        body = OrderIn(**raw)
        t = Ticket(
            legs=[TicketLeg.parse(x.symbol, x.quantity, x.effect) for x in body.legs],
            intent=body.intent,
            duration=body.duration,
            limit_net=body.limit_net,
            trade_id=body.trade_id,
        )
        apply_quotes(t, self.quotes)
        p = prices(t)
        if not place:
            return {"errors": [], "prices": p}
        return await self.broker.place(t, self.fill_quotes)

    async def open_strangle(self, profit_target=None):
        t = Ticket(
            legs=[
                TicketLeg.parse(sym("P", 555), -1, "open"),
                TicketLeg.parse(sym("C", 610), -1, "open"),
            ],
            limit_net=5.80,
            profit_target=profit_target,
        )
        q0 = {
            "SPY": {"quote": {"mark": 590.0}},
            sym("P", 555): q(3.0, 3.2),
            sym("C", 610): q(2.6, 2.8),
        }
        await self.broker.place(t, q0)
        return (await self.broker.snapshot())["trades"][-1]["id"]

    def working(self):
        return [o for o in self.broker.state["orders"] if o["status"] == "WORKING"]


QUOTES = {
    "SPY": {"quote": {"mark": 590.0}},
    sym("P", 555): q(2.0, 2.2),
    sym("C", 610): q(1.8, 2.0),
    sym("P", 555, NEXT): q(4.0, 4.2),
    sym("C", 610, NEXT): q(3.6, 3.8),
}


@pytest.mark.anyio
async def test_profit_target_rests_and_is_not_duplicated():
    h = Harness([], QUOTES)
    tid = await h.open_strangle()
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [strat(tid=tid)]
    await h.svc.set_trade(tid, True)
    assert await h.svc.tick() == ["target"]
    (target,) = h.working()
    assert target["limit_net"] == -2.90 and target["duration"] == "GTC"
    assert await h.svc.tick() == []  # already resting at the right price
    assert len(h.working()) == 1


@pytest.mark.anyio
async def test_switched_off_trades_and_a_pause_are_left_alone():
    h = Harness([], QUOTES)
    tid = await h.open_strangle()
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [strat(tid=tid)]
    assert await h.svc.tick() == []
    await h.svc.set_trade(tid, True)
    await h.svc.set_paused(True)
    assert await h.svc.tick() == []
    await h.svc.set_paused(False)
    h.portfolio.snapshot["data"]["quotes_at"] = time.time() - 600
    assert await h.svc.tick() == ["paused: stale"]
    assert (await h.svc.view())["paused"] is True


@pytest.mark.anyio
async def test_dte_roll_is_proposed_once_then_placed_on_accept():
    h = Harness([], QUOTES)
    tid = await h.open_strangle()
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [strat(tid=tid, dte=20)]
    await h.svc.set_trade(tid, True, {"profit": "off"})
    assert await h.svc.tick() == ["propose dte"]
    assert await h.svc.tick() == []
    (p,) = (await h.svc.view())["proposals"]
    legs = p["bodies"][0]["legs"]
    assert {x["symbol"] for x in legs if x["effect"] == "open"} == {
        sym("P", 555, NEXT),
        sym("C", 610, NEXT),
    }
    assert len([m for m in h.notifier.sent if "Proposed" in m[1]]) == 1
    assert await h.svc.accept(p["id"]) == ["place dte"]
    roll = h.broker.state["orders"][-1]
    assert roll["intent"] == "roll" and roll["limit_net"] == pytest.approx(3.80)
    assert (await h.svc.view())["proposals"] == []
    # A dismissed proposal does not come back.
    h2 = Harness([], QUOTES)
    tid2 = await h2.open_strangle()
    h2.portfolio.snapshot["underlyings"][0]["strategies"] = [strat(tid=tid2, dte=20)]
    await h2.svc.set_trade(tid2, True, {"profit": "off"})
    await h2.svc.tick()
    await h2.svc.dismiss((await h2.svc.view())["proposals"][0]["id"])
    assert await h2.svc.tick() == []


@pytest.mark.anyio
async def test_auto_close_steps_toward_natural_then_gives_up():
    # Previews see mid 4.00 debit; fills need 4.10, so the order has to chase.
    fill = {**QUOTES, sym("P", 555): q(2.0, 2.4), sym("C", 610): q(1.8, 2.0)}
    h = Harness([], QUOTES, fill_quotes=fill)
    tid = await h.open_strangle(profit_target=0.5)
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [strat(tid=tid, flags=["loss"])]
    await h.svc.set_trade(tid, True, {"loss": "auto"})
    assert "place loss" in await h.svc.tick()
    (order,) = h.working()  # the closing order replaced the profit target
    assert order["limit_net"] == -4.00 and order["duration"] == "DAY"
    steps = []
    for _ in range(10):
        for info in h.svc.state["orders"].values():
            info["stepped_at"] = 0
        steps += await h.svc.tick()
    # Natural is 4.20 debit; a 25% concession allows up to 4.05: five steps, then cancel.
    assert len([x for x in steps if x.startswith("step")]) == 5
    assert any(x.startswith("cancel") for x in steps)
    # Only the profit target is back at rest.
    assert [o["duration"] for o in h.working()] == ["GTC"]
    assert any("not filled" in m[0] for m in h.notifier.sent)
    # It does not start over by itself.
    assert all(not x.startswith("place") for x in await h.svc.tick())


@pytest.mark.anyio
async def test_a_roll_without_the_minimum_credit_closes_instead():
    h = Harness([], QUOTES, settings={"min_roll_credit": 5.0, "levels": {"dte": "auto"}})
    tid = await h.open_strangle()
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [strat(tid=tid, dte=20)]
    await h.svc.set_trade(tid, True, {"profit": "off"})
    assert await h.svc.tick() == ["place dte"]
    order = h.broker.state["orders"][-1]
    assert order["intent"] == "close" and len(order["legs"]) == 2
    assert any("closing instead" in x["text"] for x in h.svc.state["log"])


@pytest.mark.anyio
async def test_daily_cap_pauses_automation():
    h = Harness([], QUOTES, settings={"daily_cap": 1})
    tid = await h.open_strangle()
    tid2 = await h.open_strangle()
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [
        strat(tid=tid, flags=["loss"]),
        strat(tid=tid2, flags=["loss"]),
    ]
    for t in (tid, tid2):
        await h.svc.set_trade(t, True, {"loss": "auto", "profit": "off"})
    done = await h.svc.tick()
    assert done.count("place loss") == 1
    assert (await h.svc.view())["paused"] is True


def test_action_is_a_plain_record():
    assert Action("loss", "close", "x").kind == "close"
