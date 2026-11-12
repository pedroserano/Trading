from datetime import date

import pytest

from app.live import LiveBroker, normalize
from app.options import option_symbol
from app.orders import Ticket, TicketLeg, schwab_order
from app.store import MemoryStore

EXP = date(2026, 12, 18)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def sym(pc, k, u="SPY"):
    return option_symbol(u, EXP, pc, k)


class FakeSchwab:
    """Places, replaces and cancels orders the way Schwab answers them."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.next_id = 1000

    def _new(self, body, status="WORKING") -> str:
        self.next_id += 1
        oid = str(self.next_id)
        self.orders[oid] = {
            **body,
            "orderId": int(oid),
            "status": status,
            "enteredTime": "2026-11-11T15:00:00+0000",
            "orderLegCollection": [
                {**x, "legId": i + 1} for i, x in enumerate(body["orderLegCollection"])
            ],
        }
        return oid

    async def place_order(self, account, body):
        self.calls.append(("place", account))
        return self._new(body)

    async def replace_order(self, account, order_id, body):
        self.calls.append(("replace", order_id))
        self.orders[order_id]["status"] = "REPLACED"
        return self._new(body)

    async def cancel_order(self, account, order_id):
        self.calls.append(("cancel", order_id))
        self.orders[order_id]["status"] = "CANCELED"

    def fill(self, oid, prices):
        o = self.orders[oid]
        o["status"] = "FILLED"
        o["closeTime"] = "2026-11-11T15:01:00+0000"
        o["orderActivityCollection"] = [
            {
                "executionType": "FILL",
                "executionLegs": [
                    {"legId": x["legId"], "price": p, "quantity": x["quantity"]}
                    for x, p in zip(o["orderLegCollection"], prices, strict=True)
                ],
            }
        ]


class Source:
    def __init__(self, schwab):
        self.schwab = schwab

    async def orders(self, account, start, end):
        return list(self.schwab.orders.values())


class Notifier:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send(self, title, message, url="", priority=0):
        self.sent.append(title)


async def account():
    return "HASH"


def broker(allowed=True, demo=False):
    schwab = FakeSchwab()
    store = MemoryStore()
    b = LiveBroker(store, schwab, Source(schwab), account, allowed, Notifier(), demo=demo)
    return b, schwab, store


def ticket(legs, **kw):
    return Ticket(legs=[TicketLeg.parse(*x) for x in legs], **kw)


STRANGLE = [(sym("P", 555), -1, "open"), (sym("C", 610), -1, "open")]


@pytest.mark.anyio
async def test_both_switches_the_allowlist_and_limits_gate_live_orders():
    b, _, _ = broker(allowed=False)
    t = ticket(STRANGLE, limit_net=5.0)
    reasons = await b.gate(t, 2000, 100_000)
    assert any("LIVE_TRADING=true" in r for r in reasons)
    assert any("switched off" in r for r in reasons)
    b, _, _ = broker()
    await b.set_config({"enabled": "on"})
    assert await b.gate(t, 2000, 100_000) == []
    assert (await b.status())["on"] is True
    big = ticket([(sym("P", 555), -2, "open")], limit_net=5.0)
    assert any("allows 1" in r for r in await b.gate(big, 2000, 100_000))
    assert any("net liq" in r for r in await b.gate(t, 9000, 100_000))
    qqq = ticket([(option_symbol("QQQ", EXP, "P", 480), -1, "open")], limit_net=2.0)
    assert any("not on the live list" in r for r in await b.gate(qqq, 1000, 100_000))
    demo, _, _ = broker(demo=True)
    await demo.set_config({"enabled": "on"})
    assert any("Demo mode" in r for r in await demo.gate(t, 2000, 100_000))
    with pytest.raises(ValueError, match="between 1 and 50"):
        await b.set_config({"enabled": "on", "max_contracts": "0"})


@pytest.mark.anyio
async def test_place_fill_then_profit_target_rests_at_schwab():
    b, schwab, _ = broker()
    await b.set_config({"enabled": "on"})
    t = ticket(STRANGLE, limit_net=5.80, profit_target=0.5)
    order = await b.place(t, profit_target=0.5)
    assert order["status"] == "WORKING" and schwab.calls == [("place", "HASH")]
    assert schwab.orders[order["id"]]["orderType"] == "NET_CREDIT"
    working = await b.refresh()
    assert working[0]["placed_by_app"] and working[0]["limit_net"] == 5.80
    schwab.fill(order["id"], [3.10, 2.70])
    orders = await b.refresh()
    filled = next(o for o in orders if o["id"] == order["id"])
    assert filled["status"] == "FILLED" and filled["fill_net"] == 5.80
    target = next(o for o in orders if o["id"] != order["id"]) if len(orders) > 1 else None
    target = target or next(o for o in await b.refresh() if o["id"] != order["id"])
    raw = schwab.orders[target["id"]]
    assert raw["duration"] == "GOOD_TILL_CANCEL" and raw["orderType"] == "NET_DEBIT"
    assert raw["price"] == "2.90"
    assert [x["instruction"] for x in raw["orderLegCollection"]] == ["BUY_TO_CLOSE"] * 2
    assert "[Live] Profit target placed" in b.notifier.sent
    # A second refresh does not place another.
    await b.refresh()
    assert sum(1 for c in schwab.calls if c[0] == "place") == 2


@pytest.mark.anyio
async def test_a_new_close_replaces_the_working_close_on_the_same_contracts():
    b, schwab, _ = broker()
    await b.set_config({"enabled": "on"})
    close = [(sym("P", 555), 1, "close"), (sym("C", 610), 1, "close")]
    first = await b.place(ticket(close, intent="close", duration="GTC", limit_net=-2.90))
    second = await b.place(ticket(close, intent="close", limit_net=-3.50))
    assert ("replace", first["id"]) in schwab.calls and second["replaces"] == [first["id"]]
    assert schwab.orders[first["id"]]["status"] == "REPLACED"
    assert [o["id"] for o in await b.working()] == [second["id"]]
    await b.cancel(second["id"])
    assert schwab.orders[second["id"]]["status"] == "CANCELED"
    assert [a["action"] for a in await b.audit()][:3] == ["cancel", "replace", "place"]


def test_normalize_reads_prices_both_ways():
    credit = schwab_order(ticket(STRANGLE, limit_net=1.25))
    debit = schwab_order(ticket([(sym("P", 555), 1, "close")], limit_net=-0.40))
    assert normalize({**credit, "orderId": 1, "status": "QUEUED"})["limit_net"] == 1.25
    single = normalize({**debit, "orderId": 2, "status": "FILLED"})
    assert single["limit_net"] == -0.40 and single["intent"] == "close"
    assert normalize({**credit, "orderId": 1, "status": "QUEUED"})["status"] == "WORKING"


@pytest.mark.anyio
async def test_any_underlying_and_the_auto_switches():
    b, _, _ = broker()
    qqq = ticket([(option_symbol("QQQ", EXP, "P", 480), -1, "open")], limit_net=2.0)
    await b.set_config({"enabled": "on", "all_symbols": "on"})
    assert await b.gate(qqq, 1000, 100_000) == []
    # Auto needs LIVE_AUTO on the server, live trading on, and Allow Auto in Settings.
    c = await b.set_config({"enabled": "on", "auto": "on"})
    assert b.auto_cap(c) == "propose"  # no LIVE_AUTO
    store, schwab = MemoryStore(), FakeSchwab()
    a = LiveBroker(store, schwab, Source(schwab), account, True, Notifier(), server_auto=True)
    assert a.auto_cap(await a.set_config({"enabled": "on"})) == "propose"
    assert a.auto_cap(await a.set_config({"enabled": "on", "auto": "on"})) == "auto"
    assert a.auto_cap(await a.set_config({"auto": "on"})) == "propose"  # live trading off
    off = LiveBroker(store, schwab, Source(schwab), account, False, Notifier(), server_auto=True)
    assert off.auto_cap(await off.config()) == "propose"  # LIVE_TRADING off
    assert (await a.status())["server_auto"] is True
    # The Settings page's one control sets both.
    c = await a.set_config({"mode": "auto"})
    assert c["enabled"] and c["auto"] and a.auto_cap(c) == "auto"
    c = await a.set_config({"mode": "manual"})
    assert c["enabled"] and not c["auto"]
    c = await a.set_config({"mode": "off"})
    assert not c["enabled"] and not c["auto"]
    with pytest.raises(ValueError, match="Schwab orders"):
        await a.set_config({"mode": "yolo"})
