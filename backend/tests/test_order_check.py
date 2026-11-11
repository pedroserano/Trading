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
        "complexOrderStrategyType": "DIAGONAL",
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
    assert by_field["complexOrderStrategyType"]["schwab"] == "DIAGONAL"
    assert by_field["complexOrderStrategyType"]["app"] == "CALENDAR"
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
    assert view["counts"] == {"match": 3, "not comparable": 1}
    assert [e["order_id"] for e in view["entries"]][:2] == [9001, 9002]
    # A second pass keeps when each was first seen.
    first = {e["order_id"]: e["first_seen"] for e in view["entries"]}
    await svc.sync()
    assert {e["order_id"]: e["first_seen"] for e in (await svc.view())["entries"]} == first


def test_a_real_thinkorswim_roll_matches():
    """A same-strike roll placed in thinkorswim, as Schwab returned it (account removed)."""

    def leg(symbol, instruction, cusip):
        return {
            "legId": 1,
            "quantity": 2,
            "instruction": instruction,
            "orderLegType": "OPTION",
            "positionEffect": "OPENING" if instruction.endswith("OPEN") else "CLOSING",
            "instrument": {
                "type": "VANILLA",
                "cusip": cusip,
                "symbol": symbol,
                "putCall": "CALL",
                "assetType": "OPTION",
                "uniformSymbol": symbol,
                "underlyingSymbol": "PYPL",
            },
        }

    order = {
        "tag": "API_TOS:IPHONE",
        "price": 0.27,
        "status": "PENDING_ACTIVATION",
        "orderId": 1,
        "session": "NORMAL",
        "duration": "DAY",
        "editable": True,
        "quantity": 2,
        "orderType": "NET_CREDIT",
        "cancelable": True,
        "enteredTime": "2026-11-11T06:57:08+0000",
        "filledQuantity": 0,
        "orderStrategyType": "SINGLE",
        "remainingQuantity": 2,
        "orderLegCollection": [
            leg("PYPL  290119C00065000", "SELL_TO_OPEN", "0PYPL.AJ90065000"),
            leg("PYPL  281215C00065000", "BUY_TO_CLOSE", "0PYPL.LF80065000"),
        ],
        "destinationLinkName": "AutoRoute",
        "requestedDestination": "AUTO",
        "complexOrderStrategyType": "CALENDAR",
    }
    entry = oc.check(order)
    assert entry["result"] == "match", entry["diffs"]
    assert entry["diffs"] == []


def test_a_real_thinkorswim_roll_to_another_strike_is_a_diagonal():
    """A roll to another strike and an earlier expiration, as Schwab returned it."""

    def leg(symbol, instruction):
        return {
            "quantity": 4,
            "instruction": instruction,
            "orderLegType": "OPTION",
            "instrument": {
                "symbol": symbol,
                "putCall": "CALL",
                "assetType": "OPTION",
                "underlyingSymbol": "GLD",
            },
        }

    order = {
        "tag": "API_TOS:IPHONE",
        "price": 0.18,
        "status": "CANCELED",
        "orderId": 2,
        "session": "NORMAL",
        "duration": "DAY",
        "quantity": 4,
        "orderType": "NET_CREDIT",
        "closeTime": "2026-11-06T14:32:15+0000",
        "enteredTime": "2026-11-06T12:56:59+0000",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "DIAGONAL",
        "destinationLinkName": "CDRG",
        "requestedDestination": "AUTO",
        "orderActivityCollection": [{"activityType": "EXECUTION", "executionType": "CANCELED"}],
        "orderLegCollection": [
            leg("GLD   261109C00380000", "BUY_TO_CLOSE"),
            leg("GLD   261106C00375000", "SELL_TO_OPEN"),
        ],
    }
    entry = oc.check(order)
    assert entry["result"] == "match", entry["diffs"]


def test_a_real_thinkorswim_broken_wing_butterfly_close_matches():
    """Closing a 1230/1250/1300 call butterfly: unequal wings, still BUTTERFLY."""

    def leg(symbol, instruction, n):
        return {
            "quantity": n,
            "instruction": instruction,
            "orderLegType": "OPTION",
            "instrument": {
                "symbol": symbol,
                "putCall": "CALL",
                "assetType": "OPTION",
                "underlyingSymbol": "MU",
            },
        }

    order = {
        "tag": "API_TOS:IPHONE",
        "price": 0.6,
        "status": "CANCELED",
        "orderId": 3,
        "session": "NORMAL",
        "duration": "DAY",
        "quantity": 2,
        "orderType": "NET_DEBIT",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "BUTTERFLY",
        "destinationLinkName": "JNST",
        "requestedDestination": "AUTO",
        "orderLegCollection": [
            leg("MU    261120C01230000", "SELL_TO_CLOSE", 2),
            leg("MU    261120C01250000", "BUY_TO_CLOSE", 4),
            leg("MU    261120C01300000", "SELL_TO_CLOSE", 2),
        ],
    }
    entry = oc.check(order)
    assert entry["result"] == "match", entry["diffs"]


def test_butterfly_needs_the_one_two_one_shape():
    from app.orders import complex_type

    def t(*legs):
        return Ticket(legs=[TicketLeg.parse(*x) for x in legs], limit_net=1.0)

    fly = t((sym("P", 560), 1, "open"), (sym("P", 570), -2, "open"), (sym("P", 580), 1, "open"))
    assert complex_type(fly) == "BUTTERFLY"
    short = t((sym("C", 600), -1, "open"), (sym("C", 610), 2, "open"), (sym("C", 640), -1, "open"))
    assert complex_type(short) == "BUTTERFLY"
    ratio = t((sym("P", 560), 1, "open"), (sym("P", 570), -3, "open"), (sym("P", 580), 1, "open"))
    assert complex_type(ratio) != "BUTTERFLY"
    mixed = t((sym("P", 560), 1, "open"), (sym("C", 570), -2, "open"), (sym("C", 580), 1, "open"))
    assert complex_type(mixed) != "BUTTERFLY"
