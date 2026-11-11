from datetime import date

import pytest

from app import order_check as oc
from app.datasource import DemoDataSource
from app.options import option_symbol
from app.orders import Ticket, TicketLeg, schwab_order
from app.store import MemoryStore

EXP = date(2026, 12, 18)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def sym(pc, k, exp=EXP, u="SPY"):
    return option_symbol(u, exp, pc, k)


def as_returned(body: dict, **extra) -> dict:
    """An app body as Schwab's order list returns it: numbers, extra response fields."""
    out = {
        **body,
        "orderId": 1,
        "status": "CANCELED",
        "enteredTime": "2026-11-11T15:00:00+0000",
        "cancelable": False,
        "filledQuantity": 0.0,
        "quantity": float(body.get("quantity", 1)),
    }
    if "price" in body:
        out["price"] = float(body["price"])
    out["orderLegCollection"] = [
        {
            **x,
            "quantity": float(x["quantity"]),
            "legId": i + 1,
            "positionEffect": "OPENING",
            "instrument": {**x["instrument"], "underlyingSymbol": "SPY", "cusip": "0SPY.."},
        }
        for i, x in enumerate(body["orderLegCollection"])
    ]
    out.update(extra)
    return out


@pytest.mark.parametrize(
    "legs,limit,duration",
    [
        ([(sym("P", 575), -2, "open"), (sym("P", 570), 2, "open")], 1.05, "DAY"),
        ([(sym("P", 575), 2, "close"), (sym("P", 570), -2, "close")], -0.40, "GTC"),
        ([(sym("P", 555), -1, "open"), (sym("C", 610), -1, "open")], 5.80, "DAY"),
        ([(sym("P", 555), 1, "close")], -0.05, "GTC"),
        (
            [
                (sym("P", 550), 1, "open"),
                (sym("P", 555), -1, "open"),
                (sym("C", 610), -1, "open"),
                (sym("C", 615), 1, "open"),
            ],
            1.80,
            "DAY",
        ),
    ],
)
def test_the_apps_own_orders_come_back_as_matches(legs, limit, duration):
    t = Ticket(legs=[TicketLeg.parse(*x) for x in legs], limit_net=limit, duration=duration)
    entry = oc.check(as_returned(schwab_order(t)))
    assert entry["result"] == "match", entry["diffs"]
    assert entry["app"] == schwab_order(t)


def test_differences_are_listed_field_by_field():
    roll = {
        "orderType": "NET_CREDIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "CALENDAR",
        "price": 0.85,
        "quantity": 1.0,
        "taxLotMethod": "FIFO",
        "orderLegCollection": [
            {
                "instruction": "SELL_TO_OPEN",
                "quantity": 1.0,
                "instrument": {"symbol": sym("P", 555, date(2027, 1, 15)), "assetType": "OPTION"},
            },
            {
                "instruction": "BUY_TO_CLOSE",
                "quantity": 1.0,
                "instrument": {"symbol": sym("P", 555), "assetType": "OPTION"},
            },
        ],
    }
    entry = oc.check(roll)
    assert entry["result"] == "differs"
    by_field = {d["field"]: d for d in entry["diffs"]}
    assert by_field["complexOrderStrategyType"]["schwab"] == "CALENDAR"
    assert by_field["complexOrderStrategyType"]["app"] == "CUSTOM"
    assert by_field["taxLotMethod"]["level"] == "note"
    assert "price" not in by_field  # 0.85 credit both ways


def test_orders_the_app_does_not_build_are_skipped_with_a_reason():
    base = {
        "orderType": "LIMIT",
        "orderStrategyType": "SINGLE",
        "price": 1,
        "orderLegCollection": [
            {
                "instruction": "BUY",
                "quantity": 100,
                "instrument": {"symbol": "SPY", "assetType": "EQUITY"},
            }
        ],
    }
    assert "equity" in oc.check(base)["reason"]
    assert "conditional" in oc.check({**base, "orderStrategyType": "OCO"})["reason"]
    assert "limit orders" in oc.check({**base, "orderType": "MARKET"})["reason"]


@pytest.mark.anyio
async def test_service_logs_every_order_and_keeps_them():
    store = MemoryStore()
    src = DemoDataSource(today=date(2026, 11, 11))

    async def account():
        return DemoDataSource.HASH

    svc = oc.OrderCheckService(src, store, account)
    assert await svc.sync() == 4
    view = await svc.view()
    assert view["counts"] == {"match": 2, "differs": 1, "not comparable": 1}
    assert [e["order_id"] for e in view["entries"]][:2] == [9001, 9002]
    # A second pass keeps when each was first seen.
    first = {e["order_id"]: e["first_seen"] for e in view["entries"]}
    await svc.sync()
    assert {e["order_id"]: e["first_seen"] for e in (await svc.view())["entries"]} == first
