import time
from datetime import date, datetime

import pytest

from app import automation as auto
from app.automation import Action, AutomationService, decide, merged_settings, parse_settings, sides
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
    assert ("dte", "roll") in kinds(decide(strat(dte=20, rolls=3), 590, r, s, ALL))  # no cap
    capped = {**s, "max_rolls": 3}
    assert ("dte", "close") in kinds(decide(strat(dte=20, rolls=3), 590, r, capped, ALL))
    # IV rank decides between rolling and closing.
    low = decide(strat(dte=20), 590, r, s, ALL, ivr=12)
    assert ("dte", "close") in kinds(low) and "IV rank 12" in low[0].reason
    assert ("dte", "roll") in kinds(decide(strat(dte=20), 590, r, s, ALL, ivr=40))
    assert ("tested", "roll_untested") in kinds(decide(strat(flags=["tested"]), 590, r, s, ALL))
    ic = strat(name="Iron condor", flags=["tested"], defined=True)
    assert ("tested", "roll_untested_vertical") in kinds(decide(ic, 590, r, s, ALL))
    no_ic = {**s, "roll_ic_untested": False}
    assert ("tested", "notify") in kinds(decide(ic, 590, r, no_ic, ALL))
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
    h = Harness([], QUOTES, settings={"levels": {"profit": "off"}})
    tid = await h.open_strangle()
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [strat(tid=tid, dte=20)]
    await h.svc.set_trade(tid, True)
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
    h2 = Harness([], QUOTES, settings={"levels": {"profit": "off"}})
    tid2 = await h2.open_strangle()
    h2.portfolio.snapshot["underlyings"][0]["strategies"] = [strat(tid=tid2, dte=20)]
    await h2.svc.set_trade(tid2, True)
    await h2.svc.tick()
    await h2.svc.dismiss((await h2.svc.view())["proposals"][0]["id"])
    assert await h2.svc.tick() == []


@pytest.mark.anyio
async def test_auto_close_steps_toward_natural_then_gives_up():
    # Previews see mid 4.00 debit; fills need 4.10, so the order has to chase.
    fill = {**QUOTES, sym("P", 555): q(2.0, 2.4), sym("C", 610): q(1.8, 2.0)}
    h = Harness([], QUOTES, fill_quotes=fill, settings={"levels": {"loss": "auto"}})
    tid = await h.open_strangle(profit_target=0.5)
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [strat(tid=tid, flags=["loss"])]
    await h.svc.set_trade(tid, True)
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
    h = Harness(
        [], QUOTES, settings={"min_roll_credit": 5.0, "levels": {"dte": "auto", "profit": "off"}}
    )
    tid = await h.open_strangle()
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [strat(tid=tid, dte=20)]
    await h.svc.set_trade(tid, True)
    assert await h.svc.tick() == ["place dte"]
    order = h.broker.state["orders"][-1]
    assert order["intent"] == "close" and len(order["legs"]) == 2
    assert any("closing instead" in x["text"] for x in h.svc.state["log"])


@pytest.mark.anyio
async def test_daily_cap_pauses_automation():
    h = Harness([], QUOTES, settings={"daily_cap": 1, "levels": {"loss": "auto", "profit": "off"}})
    tid = await h.open_strangle()
    tid2 = await h.open_strangle()
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [
        strat(tid=tid, flags=["loss"]),
        strat(tid=tid2, flags=["loss"]),
    ]
    for t in (tid, tid2):
        await h.svc.set_trade(t, True)
    done = await h.svc.tick()
    assert done.count("place loss") == 1
    assert (await h.svc.view())["paused"] is True


def test_action_is_a_plain_record():
    assert Action("loss", "close", "x").kind == "close"


class WideSource(Source):
    """A chain every $5 from 540 to 640 around 590, deltas moving 0.012 a dollar."""

    async def option_chain(self, symbol, start, end, strike_count=None):
        exp = start
        key = f"{exp.isoformat()}:{(exp - NOW.date()).days}"
        puts, calls = {}, {}
        for k in range(540, 645, 5):
            pd = -max(0.02, min(0.98, 0.5 - (590 - k) * 0.012))
            cd = max(0.02, min(0.98, 0.5 - (k - 590) * 0.012))
            puts[f"{k:.1f}"] = [{"symbol": sym("P", k, exp), "bid": 1.0, "ask": 1.1, "delta": pd}]
            calls[f"{k:.1f}"] = [{"symbol": sym("C", k, exp), "bid": 1.0, "ask": 1.1, "delta": cd}]
        return {
            "underlyingPrice": 590,
            "putExpDateMap": {key: puts},
            "callExpDateMap": {key: calls},
        }


def make_tested_svc():
    h = Harness([], QUOTES)
    h.svc.source = WideSource()
    return h.svc


def opened(body):
    return sorted(x["symbol"] for x in body["legs"] if x["effect"] == "open")


@pytest.mark.anyio
async def test_untested_side_rolls_to_the_tested_delta_or_a_fixed_one():
    svc = make_tested_svc()
    await svc.load()
    st = strat(legs=[leg("P", 555, -1, -0.08), leg("C", 600, -1, 0.38)], flags=["tested"])
    u = {"symbol": "SPY", "price": 596}
    base = {"account": "sim", "trade_id": 1, "duration": "DAY"}
    act = Action("tested", "roll_untested", "x")
    (b,) = await svc._untested_bodies(st, u, act, merged_settings({}), base)
    assert opened(b) == [sym("P", 580)]  # 0.38 delta, matching the tested call
    (b,) = await svc._untested_bodies(
        st, u, act, merged_settings({"untested_target": "fixed"}), base
    )
    assert opened(b) == [sym("P", 575)]  # nearest 0.30


@pytest.mark.anyio
async def test_iron_condor_rolls_its_untested_vertical_at_the_same_width():
    svc = make_tested_svc()
    await svc.load()
    st = strat(
        name="Iron condor",
        defined=True,
        flags=["tested"],
        legs=[
            leg("P", 545, 1, -0.04),
            leg("P", 555, -1, -0.08),
            leg("C", 600, -1, 0.38),
            leg("C", 610, 1, 0.26),
        ],
    )
    u = {"symbol": "SPY", "price": 596}
    base = {"account": "sim", "trade_id": 1, "duration": "DAY"}
    (b,) = await svc._untested_bodies(
        st, u, Action("tested", "roll_untested_vertical", "x"), merged_settings({}), base
    )
    assert len(b["legs"]) == 4 and opened(b) == sorted([sym("P", 570), sym("P", 580)])
    closed = {x["symbol"]: x["quantity"] for x in b["legs"] if x["effect"] == "close"}
    assert closed == {sym("P", 555): 1, sym("P", 545): -1}


@pytest.mark.anyio
async def test_untested_rolls_stop_at_the_limit_for_the_expiration():
    svc = make_tested_svc()
    await svc.load()
    st = strat(legs=[leg("P", 555, -1, -0.08), leg("C", 600, -1, 0.38)], flags=["tested"])
    svc.state["untested"] = {f"1:{EXP.isoformat()}": 2}
    done = []
    await svc._handle(
        st,
        {"symbol": "SPY", "price": 596},
        Action("tested", "roll_untested", "x"),
        "auto",
        merged_settings({}),
        Rules(),
        done,
    )
    assert done == [] and any("roll limit" in x["text"] for x in svc.state["log"])


def test_a_lone_short_option_in_the_money_rolls_out_only_when_switched_on():
    s = merged_settings({})
    put = strat(name="Short put", legs=[leg("P", 595, -1, -0.55)], flags=["tested"])
    assert ("tested", "notify") in kinds(decide(put, 590, Rules(), s, ALL))
    on = {**s, "roll_single_itm": True}
    assert ("tested", "roll") in kinds(decide(put, 590, Rules(), on, ALL))
    # Tested by delta but not in the money: no roll out yet.
    otm = strat(name="Short put", legs=[leg("P", 585, -1, -0.35)], flags=["tested"])
    assert ("tested", "notify") in kinds(decide(otm, 590, Rules(), on, ALL))


@pytest.mark.anyio
async def test_placing_an_untested_roll_counts_toward_the_limit():
    h = Harness([], QUOTES)
    tid = await h.open_strangle()
    await h.svc.load()
    body = {
        "account": "sim",
        "trade_id": tid,
        "duration": "DAY",
        "intent": "roll",
        "_expiration": EXP.isoformat(),
        "legs": [
            {"symbol": sym("P", 555), "quantity": 1, "effect": "close"},
            {"symbol": sym("P", 555, NEXT), "quantity": -1, "effect": "open"},
        ],
    }
    done = []
    await h.svc._place_all(
        tid, Action("tested", "roll_untested", "x"), [body], merged_settings({}), done
    )
    assert done == ["place tested"]
    assert h.svc.state["untested"] == {f"{tid}:{EXP.isoformat()}": 1}


def test_tested_side_settings_from_the_form():
    from app import automation as a

    s = a.parse_settings(
        {
            "_form": "automation",
            "untested_target": "fixed",
            "roll_single_itm": "on",
            "max_untested_rolls": "3",
        },
        None,
    )
    assert s["untested_target"] == "fixed" and s["roll_single_itm"] is True
    assert s["roll_ic_untested"] is False  # unticked on that form
    assert s["max_untested_rolls"] == 3


def test_an_inverted_strangle_buys_the_guts_and_sells_the_wings():
    s = merged_settings({})
    inv = strat(legs=[leg("P", 600, -1, -0.62), leg("C", 585, -1, 0.56)], flags=["tested"])
    assert ("tested", "guts_to_wings") in kinds(decide(inv, 592, Rules(), s, ALL))
    off = {**s, "guts_to_wings": False}
    assert ("tested", "roll_untested") in kinds(decide(inv, 592, Rules(), off, ALL))
    # Not inverted: the untested side rolls as before.
    plain = strat(legs=[leg("P", 555, -1, -0.08), leg("C", 600, -1, 0.38)], flags=["tested"])
    assert ("tested", "roll_untested") in kinds(decide(plain, 596, Rules(), s, ALL))


@pytest.mark.anyio
async def test_guts_to_wings_order_and_its_debit_limit():
    svc = make_tested_svc()
    await svc.load()
    inv = strat(legs=[leg("P", 600, -1, -0.62), leg("C", 585, -1, 0.56)], flags=["tested"])
    base = {"account": "sim", "trade_id": 1, "duration": "DAY"}
    (b,) = await svc._guts_bodies(inv, {"symbol": "SPY", "price": 592}, merged_settings({}), base)
    assert opened(b) == sorted([sym("P", 560), sym("C", 620)])  # the 16-delta wings
    assert len(b["legs"]) == 4 and b["_max_debit"] == 15.25  # width 15 plus 0.25

    placed = []

    async def submit(raw, place, mid=-14.10):
        if not place:
            return {"errors": [], "prices": {"mid": submit.mid, "natural": submit.mid - 0.2}}
        placed.append(raw["limit_net"])
        return {"id": len(placed)}

    svc.submit = submit
    act = Action("tested", "guts_to_wings", "x")
    submit.mid = -16.00  # dearer than the width plus allowance: not placed
    done = []
    await svc._place_all(1, act, [b], merged_settings({}), done)
    assert placed == [] and any("over the 15.25 limit" in x["text"] for x in svc.state["log"])
    submit.mid = -14.10  # within it: placed at mid, not turned into a close
    await svc._place_all(1, act, [b], merged_settings({}), done)
    assert placed == [-14.10] and done == ["place tested"]


def test_guts_settings_from_the_form():
    from app import automation as a

    s = a.parse_settings({"_form": "automation", "guts_extra_debit": "0.5"}, None)
    assert s["guts_to_wings"] is False and s["guts_extra_debit"] == 0.5
    s = a.parse_settings({"_form": "automation", "guts_to_wings": "on"}, None)
    assert s["guts_to_wings"] is True and s["wings_delta"] == 0.16


def test_ticket_automanage_default_is_a_checkbox():
    cur = merged_settings(None)
    assert cur["ticket_automanage"] is True
    off = parse_settings({"_form": "automation"}, cur)
    assert off["ticket_automanage"] is False
    assert parse_settings({"_form": "automation", "ticket_automanage": "on"}, off)[
        "ticket_automanage"
    ]


# Expiration day ---------------------------------------------------------------------------


def test_expiry_rule_waits_for_its_close_time():
    s, r = merged_settings(None), Rules()
    zero = strat(dte=0)  # 555 put / 610 call
    at = lambda h, m, d=11: datetime(2026, 11, d, h, m, tzinfo=ET)  # noqa: E731
    # Touched mid-afternoon: a 0 DTE trade is not closed for it.
    assert not any(a.rule == "expiry" for a in decide(zero, 554, r, s, ALL, now=at(13, 0)))
    assert ("expiry", "close") in kinds(decide(zero, 554, r, s, ALL, now=at(15, 46)))
    # The day after Thanksgiving closes at 13:00: the rule acts from 12:45.
    assert not any(a.rule == "expiry" for a in decide(zero, 554, r, s, ALL, now=at(12, 40, 27)))
    assert ("expiry", "close") in kinds(decide(zero, 554, r, s, ALL, now=at(12, 46, 27)))
    # Pin margin: also close a short just out of the money, not one well out of it.
    pin = {**s, "expiry_pin_margin": 0.001}
    assert not any(a.rule == "expiry" for a in decide(zero, 555.3, r, s, ALL, now=at(15, 46)))
    (near,) = [a for a in decide(zero, 555.3, r, pin, ALL, now=at(15, 46)) if a.rule == "expiry"]
    assert "near the money" in near.reason
    assert not any(a.rule == "expiry" for a in decide(zero, 557, r, pin, ALL, now=at(15, 46)))


def test_expiry_close_settings_are_validated():
    s = parse_settings({"expiry_close_at": "15:50", "expiry_pin_margin": "0.1"}, None)
    assert (s["expiry_close_at"], s["expiry_pin_margin"]) == ("15:50", pytest.approx(0.001))
    with pytest.raises(ValueError, match="Expiration day close"):
        parse_settings({"expiry_close_at": "14:00"}, None)


@pytest.mark.anyio
async def test_after_the_window_only_the_expiry_close_acts():
    h = Harness([], QUOTES, settings={"levels": {"loss": "auto"}})
    tid = await h.open_strangle()
    h.portfolio.snapshot["underlyings"][0]["price"] = 550.0  # the 555 put is in the money
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [strat(tid=tid, dte=0, flags=["loss"])]
    await h.svc.set_trade(tid, True)
    h.svc.clock = lambda: datetime(2026, 11, 11, 15, 50, tzinfo=ET)  # window ends 15:30
    assert await h.svc.tick() == ["propose expiry"]
    h.svc.clock = lambda: datetime(2026, 11, 11, 16, 0, tzinfo=ET)
    assert await h.svc.tick() == []


def test_every_level_has_a_hint_with_the_current_values():
    s = {**merged_settings(None), "expiry_close_at": "15:50", "expiry_pin_margin": 0.001}
    hints = auto.rule_hints(Rules(loss_multiple=3), s)
    assert set(hints) == set(auto.RULES)
    assert "15:50" in hints["expiry"] and "0.1%" in hints["expiry"]
    assert "3x" in hints["loss"] and "50%" in hints["profit"] and "21 DTE" in hints["dte"]


# Every tastylive trade ---------------------------------------------------------------------


def test_profit_targets_per_kind_of_trade():
    r = Rules()
    strangle = strat(open_value=580.0)
    assert auto.target_limit(strangle, r) == -2.90  # buy back at half the credit
    ratio = strat(
        name="Put ratio 1×2",
        legs=[leg("P", 580, 1), leg("P", 575, -2)],
        open_value=50.0,
    )
    ratio["numbers"]["max_profit"] = 550.0
    assert auto.target_limit(ratio, r) == 0.05  # 10% of 550 made: close for a 0.05 credit
    cal = strat(name="Calendar", open_value=-200.0, legs=[leg("P", 580, -1), leg("P", 580, 1)])
    assert auto.target_limit(cal, r) == 2.20  # 10% on the 2.00 debit
    cc = strat(name="Covered call", open_value=-19800.0, legs=[leg("C", 610, -1)])
    cc["numbers"]["option_open_value"] = 200.0
    assert auto.target_limit(cc, r) == -1.00
    dv = strat(name="Put vertical", open_value=-200.0, legs=[leg("P", 580, 1), leg("P", 575, -1)])
    assert auto.target_limit(dv, r) is None
    assert not any(a.rule == "profit" for a in decide(dv, 590, r, merged_settings(None), ALL))


def test_calendars_close_at_dte_whatever_the_defined_risk_action():
    s = {**merged_settings(None), "dte_defined_action": "roll"}
    cal = strat(name="Calendar", dte=20, defined=True, open_value=-200.0)
    assert ("dte", "close") in kinds(decide(cal, 590, Rules(), s, ALL))


def test_tested_ratio_covered_call_and_lizard_roll_with_the_switch():
    on = {**merged_settings(None), "roll_single_itm": True}
    off = merged_settings(None)
    ratio = strat(
        name="Put ratio 1×2",
        legs=[leg("P", 580, 1, -0.6), leg("P", 575, -2, -0.55)],
        flags=["tested"],
    )
    assert ("tested", "roll") in kinds(decide(ratio, 570, Rules(), on, ALL))
    assert ("tested", "notify") in kinds(decide(ratio, 570, Rules(), off, ALL))
    cc = strat(name="Covered call", legs=[leg("C", 580, -1, 0.7)], flags=["tested"])
    assert ("tested", "roll_calls") in kinds(decide(cc, 590, Rules(), on, ALL))
    lizard = strat(
        name="Jade lizard",
        legs=[leg("P", 590, -1, -0.6), leg("C", 610, -1, 0.1), leg("C", 612, 1, 0.08)],
        flags=["tested"],
    )
    assert ("tested", "roll_puts") in kinds(decide(lizard, 585, Rules(), on, ALL))
    # The calls being tested is no reason to roll the put.
    assert ("tested", "notify") in kinds(decide(lizard, 611, Rules(), on, ALL))


@pytest.mark.anyio
async def test_lizard_put_roll_leaves_the_call_spread():
    h = Harness([], QUOTES)
    lizard = strat(
        name="Jade lizard",
        legs=[leg("P", 555, -1, -0.6), leg("C", 610, -1, 0.1), leg("C", 615, 1, 0.08)],
    )
    u = {"symbol": "SPY", "price": 550.0}
    (body,) = await h.svc._bodies(
        lizard, u, Action("tested", "roll_puts", "x"), merged_settings(None)
    )
    assert body["intent"] == "roll"
    assert {x["symbol"] for x in body["legs"]} == {sym("P", 555), sym("P", 555, NEXT)}


def test_roll_strike_goes_as_far_out_as_a_credit_allows():
    def row(k, mark):
        return {"strike": k, "put": {"mark": mark}, "call": {"mark": mark}}

    # The old put is worth 4.00; in the new expiration 555 pays 6.00, 545 4.30, 540 3.90.
    rows = [row(540, 3.9), row(545, 4.3), row(550, 5.1), row(555, 6.0), row(560, 7.2)]
    s = merged_settings(None)
    put = {"strike": 555, "mark": 4.0}
    assert auto.roll_strike(rows, "put", put, s)["strike"] == 545  # down as far as pays
    assert auto.roll_strike(rows, "put", put, {**s, "min_roll_credit": 1.0})["strike"] == 550
    assert auto.roll_strike(rows, "put", {**put, "mark": 9.0}, s) is None  # nothing pays
    assert auto.roll_strike(rows, "put", {"strike": 555, "mark": None}, s) is None
    # A covered call goes up: the old call worth 4.00 rolls to the highest strike paying.
    calls = [row(600, 7.0), row(605, 5.0), row(610, 4.2), row(615, 3.0)]
    assert auto.roll_strike(calls, "call", {"strike": 600, "mark": 4.0}, s)["strike"] == 610


class PricedSource(Source):
    """Puts priced by strike: 555 at 6.00, 550 at 5.10, 545 at 4.30, 540 at 3.90."""

    async def option_chain(self, symbol, start, end, strike_count=None):
        exp = start
        key = f"{exp.isoformat()}:{(exp - NOW.date()).days}"
        marks = {540: 3.9, 545: 4.3, 550: 5.1, 555: 6.0}

        def c(k, m):
            return [{"symbol": sym("P", k, exp), "bid": m - 0.05, "ask": m + 0.05, "delta": -0.3}]

        return {
            "underlyingPrice": 550,
            "putExpDateMap": {key: {f"{k:.1f}": c(k, m) for k, m in marks.items()}},
            "callExpDateMap": {},
        }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("choice", "rule", "strike"),
    [("further", "tested", 545), ("same", "tested", 555), ("further", "dte", 555)],
)
async def test_single_short_roll_strike_is_a_setting(choice, rule, strike):
    h = Harness([], QUOTES)
    h.svc.source = PricedSource()
    put = {**leg("P", 555, -1, -0.6), "mark": 4.0}
    st = strat(name="Short put", legs=[put])
    s = {**merged_settings(None), "single_roll_strike": choice}
    (body,) = await h.svc._bodies(
        st, {"symbol": "SPY", "price": 550.0}, Action(rule, "roll", "x"), s
    )
    opened = [x["symbol"] for x in body["legs"] if x["effect"] == "open"]
    assert opened == [sym("P", strike, NEXT)]


def test_single_roll_strike_setting_is_validated():
    assert (
        parse_settings({"single_roll_strike": "further"}, None)["single_roll_strike"] == "further"
    )
    with pytest.raises(ValueError, match="unknown choice"):
        parse_settings({"single_roll_strike": "sideways"}, None)


def test_rules_map_follows_the_settings():
    s = merged_settings(None)
    m = auto.rules_map(Rules(), s)
    names = [row[0] for row in m["trades"]]
    assert {"Strangle", "Ratio 1×2", "Jade lizard", "Covered call", "Calendar / diagonal"} <= set(
        names
    )
    assert all(len(row) == len(m["columns"]) + 1 for row in m["trades"])
    ratio = dict((row[0], row) for row in m["trades"])["Ratio 1×2"]
    assert ratio[1] == "10% of width + credit" and "switch off" in ratio[4]
    on = {**s, "roll_single_itm": True, "single_roll_strike": "further"}
    m2 = auto.rules_map(Rules(profit_target_ratio=0.2), on)
    rows = {row[0]: row for row in m2["trades"]}
    assert rows["Ratio 1×2"][1] == "20% of width + credit"
    assert "furthest strike" in rows["Short put / call"][4]
    assert m2["itm_muted"]  # rolls set up, but the Tested level only notifies
    m3 = auto.rules_map(Rules(), {**on, "levels": {**on["levels"], "tested": "propose"}})
    assert not m3["itm_muted"] and m3["level_of"]["tested"] == "propose"


@pytest.mark.anyio
async def test_live_entry_fill_switches_on_the_trade_holding_its_legs_any_time():
    # A Schwab fill can land in a trade named after an earlier one (linked as a roll), so
    # the trade is found by its legs, and this happens paused and outside the window too.
    h = Harness([], QUOTES)
    h.svc.account = "live"
    h.svc.clock = lambda: datetime(2026, 11, 11, 16, 30, tzinfo=ET)
    order = {
        "id": 777,
        "status": "FILLED",
        "intent": "open",
        "trade_id": "c-777",
        "fill_net": 1.0,
        "legs": [
            {"symbol": sym("P", 555), "quantity": -1},
            {"symbol": sym("C", 610), "quantity": -1},
        ],
    }
    h.broker.state = {"orders": [order]}
    await h.svc.watch_entry(order)
    await h.svc.set_paused(True)
    h.portfolio.snapshot["underlyings"][0]["strategies"] = []
    await h.svc.tick()  # filled, but not in the positions yet
    assert "c-123" not in h.svc.state["trades"]
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [strat(tid="c-123")]
    await h.svc.tick()
    assert h.svc.state["trades"]["c-123"] == {"enabled": True}
    assert not h.svc.state["enable_pending"]


@pytest.mark.anyio
async def test_sim_entry_fill_after_hours_is_switched_on_and_saved():
    h = Harness([], QUOTES)
    t = Ticket(
        legs=[
            TicketLeg.parse(sym("P", 555), -1, "open"),
            TicketLeg.parse(sym("C", 610), -1, "open"),
        ],
        limit_net=5.80,
    )
    order = await h.broker.place(
        t,
        {"SPY": {"quote": {"mark": 590.0}}, sym("P", 555): q(3.0, 3.2), sym("C", 610): q(2.6, 2.8)},
    )
    assert order["status"] == "FILLED"
    await h.svc.watch_entry(order)
    tid = str(order["trade_id"])
    assert h.svc.state["trades"][tid] == {"enabled": True}  # at once, no wait for a pass
    h.svc.clock = lambda: datetime(2026, 11, 11, 18, 0, tzinfo=ET)
    h.portfolio.snapshot["session"] = "closed"
    await h.svc.tick()
    assert h.svc.state["trades"][tid] == {"enabled": True}
    saved = h.store.settings[h.svc.state_key]
    assert saved["trades"][tid] == {"enabled": True}


def test_untested_roll_leans_toward_the_bias():
    t = auto.untested_target
    assert t(0.35, "P", "neutral") == 0.35 and t(0.35, "C", "neutral") == 0.35
    assert t(0.35, "P", "bullish") == 0.40  # a call tested: the put rolls up past neutral
    assert t(0.35, "C", "bullish") == 0.30  # a put tested: the call rolls a little less
    assert t(0.35, "C", "bearish") == 0.40 and t(0.35, "P", "bearish") == 0.30
    assert t(0.60, "P", "bullish") == 0.5 and t(0.12, "C", "bullish") == 0.10
    m = auto.rules_map(Rules(delta_bias="bullish"), merged_settings(None))
    assert (
        "leaning long with the trade's view, else your bullish bias"
        in (dict((r[0], r) for r in m["trades"])["Strangle"][3])
    )


@pytest.mark.anyio
async def test_a_trade_view_survives_toggling_and_steers_its_rolls():
    h = Harness([], QUOTES)
    svc = h.svc
    await svc.set_trade(7, True, view="bearish")
    await svc.set_trade(7, False)
    await svc.set_trade(7, True)
    assert svc.state["trades"]["7"] == {"enabled": True, "view": "bearish"}
    # The trade's own view beats the portfolio bias; without one, the bias.
    assert svc.trade_view(7, Rules(delta_bias="bullish")) == "bearish"
    await svc.set_trade(7, view=None)
    assert svc.trade_view(7, Rules(delta_bias="bullish")) == "bullish"
    assert svc.trade_view(7, Rules(delta_bias="contrarian")) == "neutral"
    with pytest.raises(ValueError):
        await svc.set_trade(7, view="sideways")


# Would own: assignment only with cash and sweep, never margin ---------------------------


def test_free_cash_is_cash_and_sweep_only():
    from app import cash

    assert cash.free_cash({"cash": 9_000.0, "sweep": 6_000.0, "buying_power": 90_000}) == 15_000
    assert cash.free_cash({"cash": -500.0, "sweep": 6_000.0}) == 0  # already borrowing
    assert cash.free_cash({"cash": 9_000.0, "margin_balance": 1_000.0}) == 0
    assert cash.free_cash(None) == 0
    covered, avail = cash.allot(20_000, [("a", 15_000), ("b", 8_000)])
    assert covered == {"a"} and avail["b"] == pytest.approx(20_000 - 25 - 15_000)


def test_a_would_own_put_is_left_to_assignment_only_when_covered():
    r, s = Rules(), merged_settings(None)
    put_only = strat(name="Short put", dte=0, legs=[leg("P", 555, -1)])
    assert ("expiry", "close") in kinds(decide(put_only, 550, r, s, ALL))
    assert not any(a.rule == "expiry" for a in decide(put_only, 550, r, s, ALL, assign_ok=True))
    # A short call in the money is still closed, even when the put may be assigned.
    both = strat(dte=0, legs=[leg("P", 555, -1), leg("C", 560, -1)])
    assert ("expiry", "close") in kinds(decide(both, 565, r, s, ALL, assign_ok=True))


@pytest.mark.anyio
async def test_would_own_at_expiration_uses_cash_never_margin():
    h = Harness([], QUOTES, settings={"levels": {"expiry": "propose", "profit": "off"}})
    tid = await h.open_strangle()
    put = strat(name="Short put", dte=0, tid=tid, legs=[leg("P", 555, -1)])
    h.portfolio.snapshot["underlyings"][0]["price"] = 550.0
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [put]
    await h.svc.set_trade(tid, True, view="would_own")
    h.svc.clock = lambda: datetime(2026, 11, 11, 15, 50, tzinfo=ET)
    # Plenty of buying power, but only 9,800 of cash: closed, with the reason.
    h.portfolio.snapshot["account"] = {"cash": 9_800.0, "sweep": 0.0, "buying_power": 200_000}
    assert await h.svc.tick() == ["propose expiry"]
    (p,) = (await h.svc.view())["proposals"]
    assert "55,500 is needed" in p["reason"] and "no margin used" in p["reason"]
    assert any("not enough cash" in t for t, _ in h.notifier.sent)
    # With cash and sweep covering it: left to be assigned.
    h2 = Harness([], QUOTES, settings={"levels": {"expiry": "propose", "profit": "off"}})
    tid2 = await h2.open_strangle()
    put2 = strat(name="Short put", dte=0, tid=tid2, legs=[leg("P", 555, -1)])
    h2.portfolio.snapshot["underlyings"][0]["price"] = 550.0
    h2.portfolio.snapshot["underlyings"][0]["strategies"] = [put2]
    await h2.svc.set_trade(tid2, True, view="would_own")
    h2.svc.clock = lambda: datetime(2026, 11, 11, 15, 50, tzinfo=ET)
    h2.portfolio.snapshot["account"] = {"cash": 30_000.0, "sweep": 30_000.0}
    assert await h2.svc.tick() == []
