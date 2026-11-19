from datetime import date

from app import chain as chain_mod
from app.automation import decide, merged_settings
from app.main import nonstandard_checks
from app.options import NONSTANDARD, leg_from_schwab_position, nonstandard, option_symbol
from app.orders import Ticket, TicketLeg
from app.strategies import Rules, evaluate, group

EXP = date(2027, 1, 15)
ADJ = "AAPL1 270115C00150000"  # adjusted after a corporate action
STD = option_symbol("AAPL", EXP, "C", 150)


def pos(symbol, qty, under="AAPL", **inst):
    return {
        "longQuantity": max(qty, 0),
        "shortQuantity": max(-qty, 0),
        "averagePrice": 3.0,
        "marketValue": 250.0 * qty,
        "instrument": {
            "assetType": "OPTION",
            "symbol": symbol,
            "underlyingSymbol": under,
            "putCall": "CALL",
            **inst,
        },
    }


def test_recognised_by_root_multiplier_or_deliverables():
    assert nonstandard(ADJ, "AAPL") == "adjusted option (root AAPL1)"
    assert nonstandard(STD, "AAPL") is None
    assert nonstandard(option_symbol("SPXW", EXP, "P", 5000), "$SPX") is None  # weeklies
    assert nonstandard(STD, "AAPL", {"optionMultiplier": 150}) == "multiplier 150, not 100"
    assert nonstandard(STD, "AAPL", {"optionMultiplier": 100}) is None
    odd = {
        "optionDeliverables": [
            {"symbol": "AAPL", "deliverableUnits": 150},
            {"symbol": "USD", "deliverableUnits": 25.5},
        ]
    }
    assert nonstandard(STD, "AAPL", odd) == "delivers 150 AAPL, 25.5 USD"
    plain = {"optionDeliverables": [{"symbol": "AAPL", "deliverableUnits": 100}]}
    assert nonstandard(STD, "AAPL", plain) is None


def test_a_held_one_stands_alone_without_greeks_or_management():
    ns = leg_from_schwab_position(pos(ADJ, -1))
    std_c = leg_from_schwab_position(pos(option_symbol("AAPL", EXP, "C", 250), -1))
    std_p = leg_from_schwab_position(
        {
            **pos(option_symbol("AAPL", EXP, "P", 200), -1),
            "instrument": {
                "assetType": "OPTION",
                "symbol": option_symbol("AAPL", EXP, "P", 200),
                "underlyingSymbol": "AAPL",
                "putCall": "PUT",
            },
        }
    )
    assert ns.underlying == "AAPL" and ns.extra["nonstandard"].startswith("adjusted")
    for x in (ns, std_c, std_p):
        x.delta, x.theta, x.gamma, x.vega = -0.5, 0.02, 0.01, 0.1
    sts = group([ns, std_c, std_p])
    names = sorted(s.name for s in sts)
    assert names == [NONSTANDARD, "Strangle"]  # never matched with the standard legs
    st = next(s for s in sts if s.name == NONSTANDARD)
    evaluate(st, date(2026, 11, 19), 260.0, Rules())
    n = st.numbers
    assert n["delta"] is None and n["max_profit"] is None and n["pl"] is not None
    assert [f["kind"] for f in st.flags] == ["nonstandard"]
    sj = {
        "name": st.name,
        "legs": [{"kind": "OPTION", "quantity": -1}],
        "numbers": n,
        "flags": st.flags,
        "trade_id": 7,
    }
    assert decide(sj, 260.0, Rules(), merged_settings(None), {"tested": "auto"}) == []


def test_the_ticket_blocks_opening_or_rolling_and_warns_on_a_close():
    def ticket(intent, effect, qty):
        return Ticket(legs=[TicketLeg.parse(ADJ, qty, effect)], intent=intent, limit_net=1.0)

    errors, warnings = [], []
    nonstandard_checks(ticket("close", "close", 1), {}, errors, warnings)
    assert errors == [] and "check what it delivers" in warnings[0]
    errors, warnings = [], []
    nonstandard_checks(ticket("open", "open", -1), {}, errors, warnings)
    assert "closing only" in errors[0]
    # Marked by the snapshot (multiplier or deliverables), whatever its root.
    errors = []
    t = Ticket(legs=[TicketLeg.parse(STD, -1, "open")], intent="open", limit_net=1.0)
    nonstandard_checks(t, {STD: "multiplier 150, not 100"}, errors, [])
    assert "multiplier 150" in errors[0]
    errors = []
    nonstandard_checks(t, {}, errors, [])
    assert errors == []


def test_the_chain_never_lists_them():
    def c(sym, **kw):
        return {"symbol": sym, "bid": 1.0, "ask": 1.1, "delta": 0.3, **kw}

    raw = {
        "symbol": "AAPL",
        "underlyingPrice": 150.0,
        "callExpDateMap": {
            "2027-01-15:57": {
                "150.0": [c(ADJ), c(STD)],
                "155.0": [c(option_symbol("AAPL", EXP, "C", 155), nonStandard=True)],
                "160.0": [c(option_symbol("AAPL", EXP, "C", 160), multiplier=150.0)],
            }
        },
    }
    rows = chain_mod.chain_rows(raw)["rows"]
    assert [(r["strike"], r["call"]["symbol"]) for r in rows] == [(150.0, STD)]
