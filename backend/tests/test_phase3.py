import asyncio
from datetime import date, datetime

import pytest
from fake_source import ConnectedSchwab, DemoDataSource
from fastapi.testclient import TestClient

from app import guards
from app.config import Settings
from app.main import create_app, summarize_preview
from app.notify import ET
from app.options import option_symbol
from app.orders import (
    Ticket,
    TicketError,
    TicketLeg,
    apply_quotes,
    prices,
    schwab_order,
    validate_shape,
)
from app.sim import SimBroker, stats
from app.store import MemoryStore

TODAY = date(2026, 11, 11)
EXP = date(2026, 12, 18)
NEXT = date(2027, 1, 15)
PASSWORD = "p" * 12


@pytest.fixture
def anyio_backend():
    return "asyncio"


def sym(pc, k, exp=EXP, u="SPY"):
    return option_symbol(u, exp, pc, k)


def q(bid, ask, mark=None):
    return {
        "quote": {
            "bidPrice": bid,
            "askPrice": ask,
            "mark": mark if mark is not None else round((bid + ask) / 2, 4),
        }
    }


def ticket(legs, **kw):
    return Ticket(legs=[TicketLeg.parse(s, n, e) for s, n, e in legs], **kw)


# Order JSON -----------------------------------------------------------------------------


def test_vertical_matches_schwab_documented_shape():
    t = ticket([(sym("P", 575), -2, "open"), (sym("P", 570), 2, "open")], limit_net=1.05)
    body = schwab_order(t)
    assert body == {
        "orderType": "NET_CREDIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "VERTICAL",
        "price": "1.05",
        "quantity": 2,
        "orderLegCollection": [
            {
                "instruction": "SELL_TO_OPEN",
                "quantity": 2,
                "instrument": {"symbol": "SPY   261218P00575000", "assetType": "OPTION"},
            },
            {
                "instruction": "BUY_TO_OPEN",
                "quantity": 2,
                "instrument": {"symbol": "SPY   261218P00570000", "assetType": "OPTION"},
            },
        ],
    }


def test_single_leg_debit_gtc_and_strategy_types():
    t = ticket([(sym("P", 555), 1, "close")], limit_net=-2.10, duration="GTC")
    body = schwab_order(t)
    assert (
        body["orderType"],
        body["price"],
        body["duration"],
        body["complexOrderStrategyType"],
    ) == ("LIMIT", "2.10", "GOOD_TILL_CANCEL", "NONE")
    assert body["orderLegCollection"][0]["instruction"] == "BUY_TO_CLOSE"
    assert "quantity" not in body
    ic = ticket(
        [
            (sym("P", 565), 1, "open"),
            (sym("P", 570), -1, "open"),
            (sym("C", 615), -1, "open"),
            (sym("C", 620), 1, "open"),
        ],
        limit_net=1.5,
    )
    assert schwab_order(ic)["complexOrderStrategyType"] == "IRON_CONDOR"
    strangle = ticket([(sym("P", 555), -1, "open"), (sym("C", 610), -1, "open")], limit_net=8.8)
    assert schwab_order(strangle)["complexOrderStrategyType"] == "STRANGLE"
    roll = ticket(
        [
            (sym("P", 555), 1, "close"),
            (sym("C", 610), 1, "close"),
            (sym("P", 555, NEXT), -1, "open"),
            (sym("C", 610, NEXT), -1, "open"),
        ],
        intent="roll",
        limit_net=0.85,
    )
    body = schwab_order(roll)
    assert (
        body["complexOrderStrategyType"] == "DOUBLE_DIAGONAL" and body["orderType"] == "NET_CREDIT"
    )
    # Rolls of one option are labelled as thinkorswim labels them.
    one = ticket(
        [(sym("P", 555), 1, "close"), (sym("P", 555, NEXT), -1, "open")],
        intent="roll",
        limit_net=0.3,
    )
    assert schwab_order(one)["complexOrderStrategyType"] == "CALENDAR"
    down = ticket(
        [(sym("P", 555), 1, "close"), (sym("P", 550, NEXT), -1, "open")],
        intent="roll",
        limit_net=0.1,
    )
    assert schwab_order(down)["complexOrderStrategyType"] == "DIAGONAL"
    assert [x["instruction"] for x in body["orderLegCollection"]] == [
        "BUY_TO_CLOSE",
        "BUY_TO_CLOSE",
        "SELL_TO_OPEN",
        "SELL_TO_OPEN",
    ]


def test_ticket_validation():
    with pytest.raises(TicketError):
        TicketLeg.parse("SPY", 1, "open")
    with pytest.raises(TicketError):
        TicketLeg.parse(sym("P", 555), 0, "open")
    t = ticket([(sym("P", 555), -1, "open")])
    with pytest.raises(TicketError, match="limit"):
        schwab_order(t)


def test_prices_natural_mid_far_for_ratio():
    t = ticket([(sym("P", 575), -2, "open"), (sym("P", 570), 4, "open")])
    apply_quotes(t, {sym("P", 575): q(5.80, 6.00), sym("P", 570): q(4.40, 4.60)})
    assert t.units == 2 and t.ratios() == [-1, 2]
    p = prices(t)
    # Per unit: sell one 575 and buy two 570.
    assert p == {
        "natural": round(5.80 - 2 * 4.60, 2),
        "mid": round(5.90 - 2 * 4.50, 2),
        "far": round(6.00 - 2 * 4.40, 2),
    }


# Guards ---------------------------------------------------------------------------------


def priced(limit, legs=None, **kw):
    t = ticket(
        legs or [(sym("P", 575), -1, "open"), (sym("P", 570), 1, "open")], limit_net=limit, **kw
    )
    apply_quotes(
        t, {sym("P", 575): q(5.80, 6.00), sym("P", 570): q(4.40, 4.60), sym("P", 555): q(3.0, 3.2)}
    )
    return t, prices(t)


def test_guard_price_band():
    t, p = priced(1.30)  # natural 1.20, far 1.60
    assert guards.check(t, p, TODAY, limits=guards.Limits()) == ([], [])
    t, p = priced(0.20)
    errors, _ = guards.check(t, p, TODAY, limits=guards.Limits())
    assert "worse than the natural" in errors[0]
    t, p = priced(5.00)
    assert "beyond the far side" in guards.check(t, p, TODAY, limits=guards.Limits())[0][0]


def test_guard_contracts_quotes_expiry_and_closing():
    t, p = priced(1.3, [(sym("P", 575), -20, "open"), (sym("P", 570), 20, "open")])
    assert "contract limit" in guards.check(t, p, TODAY, limits=guards.Limits())[0][0]
    t = ticket([(sym("P", 999), -1, "open")], limit_net=1.0)
    apply_quotes(t, {})
    assert any(
        "No live quote" in e for e in guards.check(t, prices(t), TODAY, limits=guards.Limits())[0]
    )
    t, p = priced(1.3)
    assert any(
        "expired" in e for e in guards.check(t, p, date(2027, 1, 1), limits=guards.Limits())[0]
    )
    t, p = priced(-3.1, [(sym("P", 555), 1, "close")], intent="close")
    errs = guards.check(t, p, TODAY, limits=guards.Limits(), held={sym("P", 555): -1})[0]
    assert errs == []
    errs = guards.check(t, p, TODAY, limits=guards.Limits(), held={sym("P", 555): 1})[0]
    assert "position holds 1" in errs[0]


def test_guard_size_warning_and_block():
    t, p = priced(1.3)
    _, w = guards.check(
        t,
        p,
        TODAY,
        limits=guards.Limits(),
        net_liq=10_000,
        bp_effect=400,
        bp_used_after=0.4,
        bp_ceiling=0.3,
    )
    assert any("4.0% of net liq" in x for x in w) and any("ceiling" in x for x in w)
    e, _ = guards.check(t, p, TODAY, limits=guards.Limits(), net_liq=1_000, bp_effect=400)
    assert "hard limit" in e[0]


# Simulated broker -----------------------------------------------------------------------


def clock(h=11, d=11):
    return lambda: datetime(2026, 11, d, h, 0, tzinfo=ET)


QUOTES = {
    "SPY": {"quote": {"mark": 590.0}},
    sym("P", 555): q(3.00, 3.20),
    sym("C", 610): q(2.60, 2.80),
}


@pytest.mark.anyio
async def test_sim_open_fill_profit_target_and_close():
    store = MemoryStore()
    b = SimBroker(store, 10_000, clock=clock())
    t = ticket(
        [(sym("P", 555), -1, "open"), (sym("C", 610), -1, "open")],
        limit_net=5.80,
        profit_target=0.5,
    )
    order = await b.place(t, QUOTES)  # mid 5.80: fills at once
    assert order["status"] == "FILLED"
    st = await b.snapshot()
    assert st["cash"] == pytest.approx(10_000 + 580 - 0.90)  # 2 contracts at 0.45
    trade = st["trades"][0]
    assert trade["status"] == "open" and trade["strategy"] == "Strangle"
    assert {p["symbol"]: p["quantity"] for p in st["positions"]} == {
        sym("P", 555): -1,
        sym("C", 610): -1,
    }
    target = next(o for o in st["orders"] if o["status"] == "WORKING")
    assert target["limit_net"] == -2.90 and target["duration"] == "GTC"
    assert target["schwab_json"]["orderType"] == "NET_DEBIT"

    # Not yet: mid debit 4.10 is worse than the 2.90 target.
    assert await b.on_quotes({**QUOTES}) == 0
    cheaper = {**QUOTES, sym("P", 555): q(1.40, 1.50), sym("C", 610): q(1.30, 1.40)}
    assert await b.on_quotes(cheaper) == 1
    st = await b.snapshot()
    assert st["positions"] == [] and st["trades"][0]["status"] == "closed"
    assert st["trades"][0]["realized_pl"] == pytest.approx(580 - 290 - 1.80)
    assert stats(st["trades"])[0]["win_rate"] == 1.0
    # Saved to the store, so it survives a restart.
    again = SimBroker(store, 1, clock=clock())
    assert (await again.load())["trades"][0]["status"] == "closed"


@pytest.mark.anyio
async def test_sim_limit_better_than_mid_waits_then_day_order_expires():
    b = SimBroker(MemoryStore(), 10_000, clock=clock(11))
    t = ticket([(sym("P", 555), -1, "open")], limit_net=3.50)
    assert (await b.place(t, QUOTES))["status"] == "WORKING"
    b.clock = clock(16, 11)
    await b.on_quotes(QUOTES)
    assert (await b.snapshot())["orders"][0]["status"] == "EXPIRED"


@pytest.mark.anyio
async def test_sim_roll_keeps_one_trade_and_cumulative_credit():
    b = SimBroker(MemoryStore(), 10_000, clock=clock())
    await b.place(ticket([(sym("P", 555), -1, "open")], limit_net=3.10), QUOTES)
    tid = (await b.snapshot())["trades"][0]["id"]
    quotes = {**QUOTES, sym("P", 555, NEXT): q(4.00, 4.20)}
    roll = ticket(
        [(sym("P", 555), 1, "close"), (sym("P", 555, NEXT), -1, "open")],
        intent="roll",
        trade_id=tid,
        limit_net=1.00,
    )
    order = await b.place(roll, quotes)
    assert order["status"] == "FILLED"
    st = await b.snapshot()
    assert len(st["trades"]) == 1 and [e["kind"] for e in st["trades"][0]["events"]] == [
        "open",
        "roll",
    ]
    info = b.trade_info()[tid]
    assert info["net"] == pytest.approx(310 + 100) and info["rolls"] == 1
    assert [p["symbol"] for p in st["positions"]] == [sym("P", 555, NEXT)]
    with pytest.raises(ValueError, match="not open"):
        await b.place(
            ticket([(sym("P", 555), 1, "close")], intent="close", trade_id=999, limit_net=-3.1),
            quotes,
        )


@pytest.mark.anyio
async def test_sim_settles_expired_options_at_intrinsic():
    b = SimBroker(MemoryStore(), 10_000, clock=clock())
    await b.place(
        ticket([(sym("P", 555), -1, "open"), (sym("C", 610), -1, "open")], limit_net=5.80), QUOTES
    )
    b.clock = lambda: datetime(2026, 12, 19, 10, tzinfo=ET)
    await b.on_quotes({"SPY": {"quote": {"mark": 550.0}}})
    st = await b.snapshot()
    assert st["positions"] == []
    trade = st["trades"][0]
    assert trade["status"] == "closed"
    kinds = sorted(e["kind"] for e in trade["events"])
    assert kinds == ["expire", "open", "settle"]
    assert trade["realized_pl"] == pytest.approx(580 - 500 - 0.90)


@pytest.mark.anyio
async def test_sim_account_view_is_schwab_shaped():
    b = SimBroker(MemoryStore(), 10_000, clock=clock())
    await b.place(
        ticket([(sym("P", 575), -1, "open"), (sym("P", 570), 1, "open")], limit_net=1.0),
        {**QUOTES, sym("P", 575): q(5.9, 6.1), sym("P", 570): q(4.9, 5.1)},
    )
    acct = b.account({**QUOTES, sym("P", 575): q(5.9, 6.1), sym("P", 570): q(4.9, 5.1)})
    sa = acct["securitiesAccount"]
    assert sa["currentBalances"]["maintenanceRequirement"] == 500  # 5-wide
    assert sa["currentBalances"]["liquidationValue"] == pytest.approx(10_000 + 100 - 0.9 - 100)
    assert {p["tradeId"] for p in sa["positions"]} == {2}


def test_summarize_preview_finds_nested_fields_and_messages():
    raw = {
        "orderStrategy": {
            "orderBalance": {"projectedCommission": 1.3, "projectedBuyingPower": 9000.5}
        },
        "orderValidationResult": {
            "warns": [{"message": "Low volume"}],
            "rejects": [{"title": "Insufficient funds"}],
        },
    }
    s = summarize_preview(raw)
    assert s["fields"] == {"projectedCommission": 1.3, "projectedBuyingPower": 9000.5}
    assert not s["ok"] and {m["level"] for m in s["messages"]} == {"warning", "error"}


def test_schwab_activity_messages_are_read_as_text():
    raw = {
        "orderValidationResult": {
            "rejects": [
                {
                    "activityMessage": "This order may result in an oversold/overbought "
                    "position in your account.",
                    "originalSeverity": "REJECT",
                }
            ]
        }
    }
    s = summarize_preview(raw)
    assert s["messages"][0]["text"].startswith("This order may result in an oversold")


@pytest.mark.anyio
async def test_an_oversold_preview_is_expected_while_a_working_close_holds_the_contracts():
    from app.main import _expected_oversold
    from app.orders import Ticket, TicketLeg

    p665, p660 = "SPY   270115P00665000", "SPY   270115P00660000"
    p700, p695 = "SPY   270115P00700000", "SPY   270115P00695000"
    c760 = "SPY   270115C00760000"
    t = Ticket(
        legs=[
            TicketLeg.parse(p665, 1, "close"),
            TicketLeg.parse(p660, -1, "close"),
            TicketLeg.parse(p700, -1, "open"),
            TicketLeg.parse(p695, 1, "open"),
        ],
        intent="roll",
        limit_net=0.40,
    )
    oversold = {
        "ok": False,
        "fields": {},
        "messages": [
            {"level": "error", "text": "This order may result in an oversold/overbought position"}
        ],
    }

    class Broker:
        def __init__(self, orders):
            self.orders = orders

        async def working(self):
            return self.orders

    target = {
        "id": "123",
        "intent": "close",
        "legs": [{"symbol": p665}, {"symbol": p660}, {"symbol": c760}],
    }
    warnings = []
    out = await _expected_oversold(oversold, t, Broker([target]), warnings)
    assert out["ok"] and out["messages"][0]["level"] == "info"
    assert "#123" in warnings[0] and "left without a closing order" in warnings[0]
    # Nothing working on those contracts: Schwab's rejection stands.
    warnings = []
    assert not (await _expected_oversold(oversold, t, Broker([]), warnings))["ok"]
    assert warnings == []
    # Any other rejection stands too.
    funds = {**oversold, "messages": [{"level": "error", "text": "Insufficient funds"}]}
    assert not (await _expected_oversold(funds, t, Broker([target]), []))["ok"]


# API ------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    s = Settings(
        _env_file=None,
        app_password=PASSWORD,
        secret_key="s" * 32,
        schwab_app_key="k",
        schwab_app_secret="s",
        cookie_secure=False,
        data_dir=tmp_path,
    )
    app = create_app(
        s,
        ConnectedSchwab(),
        store=MemoryStore(),
        start_background=False,
        static_dir=tmp_path / "none",
        source=DemoDataSource(today=TODAY),
    )
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": PASSWORD})
        yield c


def first_put(client, target=0.3):
    exps = client.get("/api/trade/SPY/expirations").json()
    ch = client.get(f"/api/trade/SPY/chain?expiration={exps['default']}").json()
    row = min(
        (r for r in ch["rows"] if r["put"]["delta"]),
        key=lambda r: abs(abs(r["put"]["delta"]) - target),
    )
    return row["put"]["symbol"]


def test_api_preview_place_activity_cancel(client):
    put = first_put(client)
    body = {
        "account": "sim",
        "intent": "open",
        "legs": [{"symbol": put, "quantity": -1, "effect": "open"}],
    }
    pv = client.post("/api/orders/preview", json=body).json()
    assert pv["errors"] == [] and pv["limit_net"] == pv["prices"]["mid"]
    assert pv["schwab_json"]["orderLegCollection"][0]["instruction"] == "SELL_TO_OPEN"
    assert pv["schwab_preview"]["ok"] is True and pv["bp_effect"] > 0, pv["schwab_preview"]
    # The ticket's profit and loss chart, as on the Trade page.
    pay = pv["payoff"]
    assert len(pay["curve"]["x"]) == len(pay["curve"]["expiry"]) > 10
    assert pay["breakevens"] == pv["breakevens"] and pay["expected_move"] > 0
    # A far-away limit rests; then cancel it.
    order = client.post("/api/orders", json={**body, "limit_net": pv["prices"]["far"]}).json()
    assert order["status"] == "WORKING"
    assert client.post(f"/api/orders/{order['id']}/cancel").json()["status"] == "CANCELED"
    # At mid it fills.
    filled = client.post(
        "/api/orders", json={**body, "limit_net": pv["limit_net"], "profit_target": 0.5}
    ).json()
    assert filled["status"] == "FILLED"
    act = client.get("/api/activity?account=sim").json()
    assert act["trades"][0]["status"] == "open"
    assert [o["status"] for o in act["orders"]][:2] == ["WORKING", "FILLED"]  # target + open
    # A fat-finger limit is refused.
    bad = client.post("/api/orders", json={**body, "limit_net": 0.01})
    assert bad.status_code == 400 and "natural" in bad.json()["detail"]
    # No live orders without LIVE_TRADING, whatever the switches say.
    live = client.post(
        "/api/orders", json={**body, "account": "live", "limit_net": pv["limit_net"]}
    )
    assert live.status_code == 403 and "LIVE_TRADING" in live.json()["detail"]


def test_api_close_needs_trade_and_reset(client):
    put = first_put(client)
    r = client.post(
        "/api/orders/preview",
        json={
            "account": "sim",
            "intent": "close",
            "legs": [{"symbol": put, "quantity": 1, "effect": "close"}],
        },
    )
    assert r.status_code == 400
    assert (
        client.post("/api/sim/reset", json={"start_cash": 50000, "confirm": "nope"}).status_code
        == 400
    )
    assert client.post("/api/sim/reset", json={"start_cash": 50000, "confirm": "RESET"}).json() == {
        "ok": True
    }
    assert client.get("/api/activity?account=sim").json()["cash"] == 50000


@pytest.mark.anyio
async def test_sim_positions_grouped_by_trade_with_credit_including_rolls():
    from app.portfolio import PortfolioService
    from app.sim import SimDataSource

    store = MemoryStore()
    demo = DemoDataSource(today=TODAY)
    b = SimBroker(store, 10_000, clock=clock())
    s = Settings(
        _env_file=None,
        app_password=PASSWORD,
        secret_key="s" * 32,
        schwab_app_key="k",
        schwab_app_secret="s",
    )
    src = SimDataSource(demo, b)

    async def resolver():
        return "SIM"

    pf = PortfolioService(
        src,
        store,
        s,
        lambda: {},
        resolver,
        "sim",
        clock=clock(),
        include_watchlist=False,
        extra_symbols=b.working_symbols,
        after_quotes=b.on_quotes,
        trade_info=b.trade_info,
    )
    exp = date(2026, 12, 18)
    legs = [
        (option_symbol("SPY", exp, "P", 560), -1, "open"),
        (option_symbol("SPY", exp, "C", 620), -1, "open"),
    ]
    t = ticket(legs)
    quotes = await demo.quotes([x.symbol for x in t.legs] + ["SPY"])
    apply_quotes(t, quotes)
    t.limit_net = prices(t)["mid"]
    await b.place(t, quotes)
    await pf.tick()
    (spy,) = pf.snapshot["underlyings"]
    (st,) = spy["strategies"]
    assert st["name"] == "Strangle" and st["trade_id"] == b.state["trades"][0]["id"]
    assert st["numbers"]["open_value"] == pytest.approx(b.state["trades"][0]["events"][0]["net"])
    assert pf.snapshot["account"]["type"] == "SIMULATED"


@pytest.mark.anyio
async def test_roll_cancels_the_old_profit_target_and_closes_are_named_for_the_trade():
    b = SimBroker(MemoryStore(), 10_000, clock=clock())
    await b.place(
        ticket(
            [(sym("P", 555), -1, "open"), (sym("C", 610), -1, "open")],
            limit_net=5.80,
            profit_target=0.5,
        ),
        QUOTES,
    )
    tid = b.state["trades"][0]["id"]
    target = next(o for o in b.state["orders"] if o["status"] == "WORKING")
    assert target["strategy"] == "Strangle"
    assert target["schwab_json"]["complexOrderStrategyType"] == "STRANGLE"
    quotes = {**QUOTES, sym("P", 555, NEXT): q(4.0, 4.2), sym("C", 610, NEXT): q(3.6, 3.8)}
    roll = ticket(
        [
            (sym("P", 555), 1, "close"),
            (sym("C", 610), 1, "close"),
            (sym("P", 555, NEXT), -1, "open"),
            (sym("C", 610, NEXT), -1, "open"),
        ],
        intent="roll",
        trade_id=tid,
        limit_net=1.90,
    )
    order = await b.place(roll, quotes)
    assert order["status"] == "FILLED" and order["strategy"] == "Strangle"
    old = next(o for o in b.state["orders"] if o["id"] == target["id"])
    assert old["status"] == "REPLACED" and old["reason"] == f"Replaced by #{order['id']}"
    assert order["replaces"] == [old["id"]]
    assert [o for o in b.state["orders"] if o["status"] == "WORKING"] == []


def test_stats_count_short_trades_as_one_day():
    trades = [
        {
            "status": "closed",
            "strategy": "Strangle",
            "realized_pl": 50.0,
            "opened_at": 0.0,
            "closed_at": 600.0,
        }
    ]
    assert stats(trades)[0]["pl_per_day"] == 50.0


def test_orders_are_limited_to_four_legs():
    five = [
        (sym("P", 560), 1, "open"),
        (sym("P", 565), -1, "open"),
        (sym("C", 615), -1, "open"),
        (sym("C", 620), 1, "open"),
        (sym("C", 630), 1, "open"),
    ]
    from app.orders import validate_shape

    with pytest.raises(TicketError, match="1 to 4 legs"):
        validate_shape(ticket(five))
    validate_shape(ticket(five[:4]))


def test_api_refuses_an_eight_leg_iron_condor_roll(client):
    legs = [
        {"symbol": sym("P", k), "quantity": q, "effect": "close"} for k, q in ((560, -1), (565, 1))
    ]
    legs += [
        {"symbol": sym("C", k), "quantity": q, "effect": "close"} for k, q in ((615, 1), (620, -1))
    ]
    legs += [
        {"symbol": sym(pc, k, NEXT), "quantity": q, "effect": "open"}
        for pc, k, q in (("P", 560, 1), ("P", 565, -1), ("C", 615, -1), ("C", 620, 1))
    ]
    r = client.post(
        "/api/orders/preview",
        json={"account": "sim", "intent": "roll", "trade_id": 1, "legs": legs},
    )
    assert r.status_code == 400 and "4 legs" in r.json()["detail"]


@pytest.mark.anyio
async def test_a_second_closing_order_replaces_the_first_but_not_other_legs():
    b = SimBroker(MemoryStore(), 10_000, clock=clock())
    quotes = {**QUOTES, sym("P", 550): q(2.00, 2.20), sym("C", 615): q(1.80, 2.00)}
    ic = [
        (sym("P", 550), 1, "open"),
        (sym("P", 555), -1, "open"),
        (sym("C", 610), -1, "open"),
        (sym("C", 615), 1, "open"),
    ]
    await b.place(ticket(ic, limit_net=1.80), quotes)  # the mid
    tid = b.state["trades"][0]["id"]
    put_close = ticket(
        [(sym("P", 550), -1, "close"), (sym("P", 555), 1, "close")],
        intent="close",
        trade_id=tid,
        limit_net=-0.50,
    )
    first = await b.place(put_close, quotes)
    assert first["status"] == "WORKING"
    call_close = ticket(
        [(sym("C", 610), 1, "close"), (sym("C", 615), -1, "close")],
        intent="close",
        trade_id=tid,
        limit_net=-0.40,
    )
    other_side = await b.place(call_close, quotes)
    assert len(await b.replaceable(tid, {sym("P", 555)})) == 1
    again = await b.place(
        ticket(
            [(sym("P", 550), -1, "close"), (sym("P", 555), 1, "close")],
            intent="close",
            trade_id=tid,
            limit_net=-0.60,
        ),
        quotes,
    )
    status = {o["id"]: o["status"] for o in b.state["orders"]}
    assert status[first["id"]] == "REPLACED" and status[again["id"]] == "WORKING"
    assert status[other_side["id"]] == "WORKING"  # the call side is not touched
    assert again["replaces"] == [first["id"]]


def test_preview_warns_which_order_a_close_replaces(client):
    put = first_put(client)
    body = {
        "account": "sim",
        "intent": "open",
        "legs": [{"symbol": put, "quantity": -1, "effect": "open"}],
    }
    pv = client.post("/api/orders/preview", json=body).json()
    client.post("/api/orders", json={**body, "limit_net": pv["limit_net"], "profit_target": 0.5})
    tid = client.get("/api/activity?account=sim").json()["trades"][0]["id"]
    close = {
        "account": "sim",
        "intent": "close",
        "trade_id": tid,
        "legs": [{"symbol": put, "quantity": 1, "effect": "close"}],
    }
    warn = client.post("/api/orders/preview", json=close).json()["warnings"]
    assert any(w.startswith("Replaces working order #") and "GTC" in w for w in warn)


@pytest.mark.anyio
async def test_sim_account_reports_what_was_held_overnight():
    b = SimBroker(MemoryStore(), 10_000, clock=clock(11, 11))
    t = ticket([(sym("P", 555), -1, "open")], limit_net=3.10)
    await b.place(t, QUOTES)
    (pos,) = b.account(QUOTES)["securitiesAccount"]["positions"]
    assert pos["shortQuantity"] == 1 and pos["previousSessionShortQuantity"] == 0
    for e in b.state["trades"][0]["events"]:
        e["at"] = b.clock().timestamp() - 3 * 86400  # filled days ago: held overnight now
    (pos,) = b.account(QUOTES)["securitiesAccount"]["positions"]
    assert pos["previousSessionShortQuantity"] == 1


def test_hard_limits_per_underlying_and_over_the_ceiling():
    t = ticket([(sym("P", 555), -1, "open")], limit_net=3.10)
    apply_quotes(t, QUOTES)
    p = prices(t)
    lim = guards.Limits()
    common = dict(limits=lim, net_liq=100_000, bp_effect=4000, defined_risk=False)
    # Over the VIX ceiling: an error when blocking, a warning otherwise.
    e, w = guards.check(
        t, p, TODAY, **common, bp_used_after=0.35, bp_ceiling=0.30, block_over_ceiling=True
    )
    assert any("ceiling" in x for x in e)
    e, w = guards.check(t, p, TODAY, **common, bp_used_after=0.35, bp_ceiling=0.30)
    assert not any("ceiling" in x for x in e) and any("ceiling" in x for x in w)
    # One underlying's buying power over its cap.
    e, _ = guards.check(t, p, TODAY, **common, underlying_bp_after=16_000, underlying_cap=0.15)
    assert any("SPY would use 16%" in x for x in e)
    e, _ = guards.check(t, p, TODAY, **common, underlying_bp_after=14_000, underlying_cap=0.15)
    assert not any("would use" in x for x in e)
    # Closing orders are never held back by these.
    c = ticket([(sym("P", 555), 1, "close")], intent="close", limit_net=-3.10)
    apply_quotes(c, QUOTES)
    e, _ = guards.check(
        c,
        prices(c),
        TODAY,
        limits=lim,
        net_liq=100_000,
        bp_used_after=0.5,
        bp_ceiling=0.3,
        block_over_ceiling=True,
        underlying_bp_after=90_000,
        underlying_cap=0.15,
    )
    assert not any("ceiling" in x or "would use" in x for x in e)


def test_chart_gives_candles_strikes_and_the_year_range(client):
    r = client.get("/api/trade/SPY/chart?account=live").json()
    c = r["candles"]
    assert len(c) > 200 and c[0]["date"] < c[-1]["date"]
    assert all(
        x["low"] <= min(x["open"], x["close"]) <= max(x["open"], x["close"]) <= x["high"] for x in c
    )
    assert r["high52"] == max(x["close"] for x in c)
    # Positions load in the background, which tests do not run: none to draw yet.
    assert r["strikes"] == [] and "earnings" in r


def test_market_strip_in_demo(client):
    r = client.get("/api/market").json()
    labels = [i["label"] for i in r["items"]]
    assert labels[:4] == ["S&P", "Nasdaq", "Russell", "Dow"] and "Bitcoin" in labels
    es = r["items"][0]
    assert es["contract"] == "/ESZ26" and es["price"] > 1000 and es["change_pct"] is not None


def test_market_chart_is_the_index_behind_each_future(client):
    r = client.get("/api/market/chart", params={"label": "S&P"}).json()
    assert r["symbol"] == "$SPX" and len(r["candles"]) > 200
    assert r["high52"] == max(x["close"] for x in r["candles"])
    assert r["strikes"] == [] and r["earnings"] is None
    assert client.get("/api/market/chart", params={"label": "VIX"}).json()["symbol"] == "$VIX"
    assert client.get("/api/market/chart", params={"label": "Gold"}).json()["symbol"] == "GLD"
    assert client.get("/api/market/chart", params={"label": "Nope"}).status_code == 404


def test_automanage_on_fill_from_the_ticket(client):
    put = first_put(client)
    body = {
        "account": "sim",
        "intent": "open",
        "legs": [{"symbol": put, "quantity": -1, "effect": "open"}],
    }
    pv = client.post("/api/orders/preview", json=body).json()
    order = client.post(
        "/api/orders", json={**body, "limit_net": pv["limit_net"], "automanage": True}
    ).json()
    assert order["status"] == "FILLED"
    act = client.get("/api/activity?account=sim").json()
    assert [o["status"] for o in act["orders"]] == ["FILLED"]  # no GTC target order
    auto = client.app.state.automation
    # The simulated account fills at once, so automanage is on as the order returns.
    tid = act["trades"][0]["id"]
    assert auto.state["trades"][str(tid)] == {"enabled": True}
    assert str(order["id"]) not in auto.state["orders"]


def test_stock_close_ticket():
    t = Ticket(legs=[TicketLeg.parse("GLD", -400, "close")], intent="close", limit_net=354.5)
    validate_shape(t)
    assert t.stock and t.multiplier == 1 and t.units == 400
    body = schwab_order(t)
    assert body["orderType"] == "LIMIT" and body["price"] == "354.50"
    (leg,) = body["orderLegCollection"]
    assert leg == {
        "instruction": "SELL",
        "quantity": 400,
        "instrument": {"symbol": "GLD", "assetType": "EQUITY"},
    }
    assert TicketLeg.parse("GLD", 100, "close").instruction == "BUY_TO_COVER"
    with pytest.raises(TicketError):
        TicketLeg.parse("GLD", 100, "open")  # never opened here
    with pytest.raises(TicketError):
        validate_shape(
            Ticket(
                legs=[
                    TicketLeg.parse("GLD", -100, "close"),
                    TicketLeg.parse(sym("C", 610), 1, "close"),
                ],
                intent="close",
            )
        )
    # Shares are not held to the contract limit.
    apply_quotes(t, {"GLD": {"quote": {"bidPrice": 354.4, "askPrice": 354.6, "mark": 354.5}}})
    errors, _ = guards.check(
        t, prices(t), date(2026, 11, 12), limits=guards.Limits(), held={"GLD": 400}
    )
    assert errors == []


def test_sim_account_refuses_stock(client):
    r = client.post(
        "/api/orders/preview",
        json={
            "account": "sim",
            "intent": "close",
            "legs": [{"symbol": "SPY", "quantity": -100, "effect": "close"}],
        },
    )
    assert r.status_code == 400 and "no stock" in r.json()["detail"]


def test_ticket_warns_on_debit_trades_and_thin_jade_lizards(client):
    exps = client.get("/api/trade/SPY/expirations").json()
    rows = client.get(f"/api/trade/SPY/chain?expiration={exps['default']}").json()["rows"]
    rows = [r for r in rows if r["put"]["delta"] and r["call"]["delta"]]

    def near(side, d):
        return min(rows, key=lambda r: abs(abs(r[side]["delta"]) - d))

    def preview(legs):
        body = {"account": "sim", "intent": "open", "legs": legs}
        return client.post("/api/orders/preview", json=body).json()["warnings"]

    def leg(r, side, q):
        return {"symbol": r[side]["symbol"], "quantity": q, "effect": "open"}

    not_tasty = "Not a tastylive trade"
    hi, lo = near("put", 0.4), near("put", 0.2)
    assert any(not_tasty in w for w in preview([leg(hi, "put", 1), leg(lo, "put", -1)]))
    assert any(not_tasty in w for w in preview([leg(hi, "put", 1)]))
    assert not any(not_tasty in w for w in preview([leg(hi, "put", -1), leg(lo, "put", 1)]))
    # A jade lizard whose credit is under its call spread's width is warned about.
    sp, sc = near("put", 0.3), near("call", 0.2)
    wide = max(rows, key=lambda r: r["strike"])
    warn = preview([leg(sp, "put", -1), leg(sc, "call", -1), leg(wide, "call", 1)])
    assert any("upside is not free of risk" in w for w in warn)


def test_views_per_underlying_and_per_trade_through_the_api(client):
    r = client.post("/api/watchlist/view", json={"symbol": "SPY", "view": "bullish"})
    assert r.status_code == 200 and r.json()["views"] == {"SPY": "bullish"}
    assert client.get("/api/watchlist").json()["views"] == {"SPY": "bullish"}
    assert (
        client.post("/api/watchlist/view", json={"symbol": "SPY", "view": "up"}).status_code == 400
    )
    assert (
        client.post("/api/watchlist/view", json={"symbol": "SPY", "view": "auto"}).json()["views"]
        == {}
    )
    v = client.post("/api/automation/trade?account=sim", json={"trade_id": 5, "view": "bearish"})
    assert v.json()["trades"]["5"] == {"enabled": False, "view": "bearish"}
    v = client.post("/api/automation/trade?account=sim", json={"trade_id": 5, "view": "portfolio"})
    assert v.json()["trades"]["5"] == {"enabled": False}
    bad = client.post("/api/automation/trade?account=sim", json={"trade_id": 5, "view": "x"})
    assert bad.status_code == 400


def test_each_schwab_account_has_its_own_routes_limits_and_orders(client):
    accts = client.get("/api/accounts/in-use").json()["accounts"]
    assert [(a["key"], a["name"]) for a in accts] == [
        ("live", "Joint"),
        ("live-5678", "Individual"),
    ]
    assert client.get("/api/snapshot?account=nope").status_code == 404
    assert client.get("/api/automation?account=live-5678").json()["account"] == "live-5678"
    act = client.get("/api/activity?account=live-5678").json()
    assert act["account"] == "live-5678" and act["name"] == "Individual"
    # Each account's own Schwab limits: Individual allows only QQQ.
    asyncio.run(client.app.state.accounts["brokers"]["live-5678"].set_config({"symbols": "QQQ"}))
    put = first_put(client)
    body = {"intent": "open", "legs": [{"symbol": put, "quantity": -1, "effect": "open"}]}
    one = client.post("/api/orders/preview", json={**body, "account": "live-5678"}).json()
    other = client.post("/api/orders/preview", json={**body, "account": "live"}).json()
    assert any("not on the live list (QQQ)" in b for b in one["live_blocks"])
    assert not any("not on the live list" in b for b in other["live_blocks"])


def test_each_schwab_account_has_its_own_levels_scanner_level_and_limits(client):
    page = client.get("/connect").text
    # Each rule's sentence has a level per account, by its Schwab name.
    assert 'id="a-live-5678-profit"' in page and 'id="a-live-profit"' in page
    assert ">Individual</span>" in page and ">Joint</span>" in page
    assert 'id="s-level_acct_live-5678"' in page
    assert 'id="l-symbols-live-5678"' in page and "Save Individual limits" in page
    client.post(
        "/automation/settings",
        data={"_form": "automation", "acct_live-5678_level_dte": "notify"},
    )
    assert (
        client.get("/api/automation?account=live-5678").json()["settings"]["levels"]["dte"]
        == "notify"
    )
    assert client.get("/api/automation?account=live").json()["settings"]["levels"]["dte"] == (
        "propose"
    )
    # No Auto on a Schwab account without LIVE_AUTO (demo: never).
    client.post("/automation/settings", data={"acct_live-5678_level_profit": "auto"})
    assert (
        client.get("/api/automation?account=live-5678").json()["settings"]["levels"]["profit"]
        == "propose"
    )
    client.post("/scanner/settings", data={"level_acct_live-5678": "notify"})
    assert client.get("/api/scanner?account=live-5678").json()["level"] == "notify"
    assert client.get("/api/scanner?account=live").json()["level"] == "off"
    client.post("/live/settings", data={"account": "live-5678", "symbols": "QQQ IWM"})
    page = client.get("/connect").text
    assert "Individual: IWM, QQQ" in page and "Joint: SPY" in page


def test_preview_warns_when_a_roll_goes_past_the_zebra(client):
    exps = client.get("/api/trade/SPY/expirations").json()["expirations"]
    dates = [e["date"] for e in exps]
    zebra_exp = exps[len(exps) // 2]["date"]
    near_exp = dates[dates.index(zebra_exp) - 1]
    later_exp = dates[dates.index(zebra_exp) + 1]

    def chain(exp):
        return client.get(f"/api/trade/SPY/chain?expiration={exp}").json()

    def at(rows, k):
        return min(rows, key=lambda r: abs(r["strike"] - k))

    def place(legs):
        body = {"account": "sim", "intent": "open", "legs": legs}
        pv = client.post("/api/orders/preview", json=body).json()
        r = client.post("/api/orders", json={**body, "limit_net": pv["limit_net"]})
        assert r.status_code == 200, r.text

    z = chain(zebra_exp)
    spot = z["underlying_price"]
    place(
        [
            {"symbol": at(z["rows"], spot - 25)["call"]["symbol"], "quantity": 2, "effect": "open"},
            {"symbol": at(z["rows"], spot)["call"]["symbol"], "quantity": -1, "effect": "open"},
        ]
    )
    call = at(chain(near_exp)["rows"], spot + 10)["call"]["symbol"]
    place([{"symbol": call, "quantity": -1, "effect": "open"}])
    call_trade = client.get("/api/activity?account=sim").json()["trades"][0]["id"]
    # Background tasks are off here: refresh the simulated positions by hand.
    client.portal.call(client.app.state.accounts["portfolios"]["sim"].tick)

    def roll_to(exp):
        new = at(chain(exp)["rows"], spot + 10)["call"]["symbol"]
        body = {
            "account": "sim",
            "intent": "roll",
            "trade_id": call_trade,
            "legs": [
                {"symbol": call, "quantity": 1, "effect": "close"},
                {"symbol": new, "quantity": -1, "effect": "open"},
            ],
        }
        r = client.post("/api/orders/preview", json=body).json()
        assert "warnings" in r, r
        return r["warnings"]

    assert any("past its ZEBRA's expiration" in w for w in roll_to(later_exp))
    assert not any("ZEBRA" in w for w in roll_to(zebra_exp))


def test_the_simulated_account_can_be_hidden_from_the_picker(client):
    assert client.get("/api/accounts/in-use").json()["hide_sim"] is False
    r = client.post("/accounts/hide-sim", data={"hide_sim": "on"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/accounts/in-use").json()["hide_sim"] is True
    client.post("/accounts/hide-sim", data={}, follow_redirects=False)
    assert client.get("/api/accounts/in-use").json()["hide_sim"] is False


def test_the_simulated_account_resets_from_settings(client):
    def post(**data):
        return client.post("/accounts/reset-sim", data=data, follow_redirects=False)

    assert post(start_cash="25000", confirm="nope").status_code == 303
    assert client.get("/api/activity?account=sim").json()["cash"] != 25000
    assert post(start_cash="-5", confirm="RESET").status_code == 303
    assert client.get("/api/activity?account=sim").json()["cash"] != -5
    assert post(start_cash="25000", confirm="RESET").status_code == 303
    assert client.get("/api/activity?account=sim").json()["cash"] == 25000
    page = client.get("/connect").text
    assert 'action="/accounts/reset-sim"' in page and 'value="25000"' in page


def test_the_ticket_shows_probability_of_profit_at_its_limit(client):
    put = first_put(client)
    body = {
        "account": "sim",
        "intent": "open",
        "legs": [{"symbol": put, "quantity": -1, "effect": "open"}],
    }
    mid = client.post("/api/orders/preview", json=body).json()
    assert 0 < mid["pop"] < 1 and len(mid["breakevens"]) == 1
    p = mid["prices"]
    low = client.post("/api/orders/preview", json={**body, "limit_net": p["natural"]}).json()
    high = client.post("/api/orders/preview", json={**body, "limit_net": p["far"]}).json()
    # More credit moves the break-even further away: a better chance of profit.
    assert high["pop"] > low["pop"]
    assert high["breakevens"][0] < low["breakevens"][0]
    # The break-even of a short put is the strike less the credit, at the limit.
    strike = mid["legs"][0]["strike"]
    assert abs(high["breakevens"][0] - (strike - p["far"])) < 0.02


def test_a_closing_ticket_has_no_probability_of_profit(client):
    put = first_put(client)
    leg = {"symbol": put, "quantity": -1, "effect": "open"}
    open_body = {"account": "sim", "intent": "open", "legs": [leg]}
    pv = client.post("/api/orders/preview", json=open_body).json()
    filled = client.post("/api/orders", json={**open_body, "limit_net": pv["limit_net"]}).json()
    close = {
        "account": "sim",
        "intent": "close",
        "trade_id": filled.get("trade_id"),
        "legs": [{"symbol": put, "quantity": 1, "effect": "close"}],
    }
    r = client.post("/api/orders/preview", json=close).json()
    assert r["pop"] is None and r["breakevens"] is None


def _quoted(bid, ask, oi=None):
    t = ticket([(sym("P", 575), -1, "open")], limit_net=1.0)
    leg = t.legs[0]
    leg.bid, leg.ask, leg.mark, leg.oi = bid, ask, (bid + ask) / 2, oi
    return t


def test_liquidity_warns_on_a_leg_over_fifty_cents_wide():
    assert guards.liquidity(_quoted(2.00, 2.50)) == []
    wide = guards.liquidity(_quoted(2.00, 2.51))
    assert len(wide) == 1 and "0.51 wide (2.00 × 2.51), over 0.50" in wide[0]


def test_liquidity_warns_on_thin_open_interest_and_is_quiet_when_unknown():
    assert guards.liquidity(_quoted(1.00, 1.05, oi=None)) == []
    assert guards.liquidity(_quoted(1.00, 1.05, oi=100)) == []
    thin = guards.liquidity(_quoted(1.00, 1.05, oi=40))
    assert thin == ["SPY Dec 18 575 Put has low liquidity"]
    under = guards.liquidity(_quoted(1.00, 1.05), atm_oi=600)
    assert under == ["SPY has low liquidity"]
    assert guards.liquidity(_quoted(1.00, 1.05), atm_oi=5000) == []


def test_liquidity_warnings_never_block_the_order(client):
    # Far out of the money: thin open interest in the demo, still placeable.
    exps = client.get("/api/trade/SPY/expirations").json()
    ch = client.get(f"/api/trade/SPY/chain?expiration={exps['default']}").json()
    row = min(ch["rows"], key=lambda r: r["strike"])
    body = {
        "account": "sim",
        "intent": "open",
        "legs": [{"symbol": row["put"]["symbol"], "quantity": -1, "effect": "open"}],
    }
    pv = client.post("/api/orders/preview", json=body).json()
    assert any(w.endswith("has low liquidity") for w in pv["warnings"])
    assert not any("liquidity" in e for e in pv["errors"])


def test_watchlist_and_chain_carry_the_liquidity_rating(client):
    exps = client.get("/api/trade/SPY/expirations").json()
    ch = client.get(f"/api/trade/SPY/chain?expiration={exps['default']}").json()
    assert 1 <= ch["liquidity"]["stars"] <= 5 and ch["liquidity"]["oi"] > 0


def test_ticket_warns_of_a_low_float(client, monkeypatch):
    async def small_aapl(self, symbols):
        return {
            s: {"marketCapFloat": 7.2e6, "sharesOutstanding": 8e6} for s in symbols if s == "AAPL"
        }

    monkeypatch.setattr(DemoDataSource, "fundamentals", small_aapl)

    def put_on(sym):
        exps = client.get(f"/api/trade/{sym}/expirations").json()
        ch = client.get(f"/api/trade/{sym}/chain?expiration={exps['default']}").json()
        row = min(
            (r for r in ch["rows"] if r["put"]["delta"]),
            key=lambda r: abs(abs(r["put"]["delta"]) - 0.3),
        )
        return row["put"]["symbol"]

    def warnings(sym, intent="open"):
        body = {
            "account": "sim",
            "intent": intent,
            "legs": [{"symbol": put_on(sym), "quantity": -1, "effect": "open"}],
        }
        return client.post("/api/orders/preview", json=body).json()["warnings"]

    assert any(w.startswith("Low float: only 7.2 million") for w in warnings("AAPL"))
    assert not any(w.startswith("Low float") for w in warnings("SPY"))


def test_earnings_dates_are_set_on_the_watchlist(client):
    r = client.post(
        "/api/earnings/set", json={"symbol": "aapl", "date": "2027-01-28", "time": "amc"}
    )
    assert r.status_code == 200 and r.json()["earnings"]["source"] == "you"
    wl = client.get("/api/watchlist").json()
    assert "provider" in wl["earnings"] and "stocks" in wl["earnings"]
    row = {x["symbol"]: x for x in wl["rows"]}
    if "AAPL" in row:
        assert row["AAPL"]["earnings_mine"] and row["AAPL"]["earnings"]["date"] == "2027-01-28"
    assert all("earnings_quiet" in x and "fund" in x for x in wl["rows"])
    bad = client.post("/api/earnings/set", json={"symbol": "AAPL", "date": "2027-02-30"})
    assert bad.status_code == 400
    r = client.post("/api/earnings/set", json={"symbol": "AAPL", "date": None})
    assert r.status_code == 200 and (r.json()["earnings"] or {}).get("source") != "you"
    assert client.post("/api/earnings/refresh").status_code == 200


def test_an_opening_order_copies_to_the_other_accounts_by_net_liq(client):
    put = first_put(client)
    order = {
        "account": "live",
        "intent": "open",
        "legs": [{"symbol": put, "quantity": -4, "effect": "open"}],
    }
    app = client.app
    pfs = app.state.accounts["portfolios"]

    def set_nl(acct, v):
        snap = pfs[acct].snapshot or {}
        pfs[acct].snapshot = {**snap, "account": {**(snap.get("account") or {}), "net_liq": v}}

    live_nl = 100_000
    set_nl("live", live_nl)
    set_nl("sim", live_nl / 2)
    r = client.post("/api/orders/copies", json={"order": order}).json()
    sim = next(c for c in r["copies"] if c["account"] == "sim")
    # Half the net liq: half the spreads, listed but not previewed until asked for.
    assert sim["account"] == "sim" and sim["scaled"] == 2 and sim["units"] == 2
    assert sim["legs"] == [{"symbol": put, "quantity": -2, "effect": "open"}]
    assert sim["preview"] is None and sim["errors"] == []
    r = client.post(
        "/api/orders/copies", json={"order": order, "preview": ["sim"], "units": {"sim": 3}}
    ).json()
    sim = next(c for c in r["copies"] if c["account"] == "sim")
    assert sim["units"] == 3 and sim["legs"][0]["quantity"] == -3
    # Previewed in its own account, with that account's guards: 3 puts are too much BP there.
    assert sim["preview"]["account"] == "sim"
    assert any("hard limit" in e for e in sim["preview"]["errors"])
    # A small account still gets one spread; its own buying-power guards decide.
    set_nl("sim", live_nl / 100)
    r = client.post("/api/orders/copies", json={"order": order, "preview": ["sim"]}).json()
    sim = next(c for c in r["copies"] if c["account"] == "sim")
    assert sim["units"] == 1 and sim["errors"] == []
    assert any("hard limit" in e for e in sim["preview"]["errors"])
    # Closing orders aren't copied.
    bad = client.post("/api/orders/copies", json={"order": {**order, "intent": "close"}})
    assert bad.status_code == 400
