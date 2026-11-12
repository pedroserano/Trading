"""Ex-dividend dates from Schwab's quotes, and the early-assignment test on short calls."""

from datetime import date

from test_automation import ALL, kinds, strat

from app import dividends as dv
from app.automation import decide, merged_settings
from app.strategies import Rules

TODAY = date(2026, 12, 10)


def q(**f):
    return {"fundamental": f}


def test_info_reads_schwab_fields_and_projects_a_past_date():
    spy = q(
        nextDivExDate="2026-12-18T00:00:00Z",
        divExDate="2026-09-19T00:00:00Z",
        divPayAmount=1.83,
        divFreq=4,
    )
    d = dv.info(spy, TODAY, "SPY")
    assert d == {
        "ex_date": "2026-12-18",
        "amount": 1.83,
        "estimated": False,
        "act_by": "2026-12-17",
    }
    # Only a past date and the annual amount: the next is projected, a quarter on.
    old = dv.info(q(divExDate="2026-11-09", divAmount=1.04, divFreq=4), TODAY, "AAPL")
    assert old["estimated"] and old["ex_date"] == "2027-02-08" and old["amount"] == 0.26
    # A Monday ex-date: assignment risk is on the Friday before.
    assert dv.info(q(nextDivExDate="2026-12-14", divPayAmount=1), TODAY, "X")["act_by"] == (
        "2026-12-11"
    )
    assert dv.info(q(), TODAY, "IWM") is None
    assert dv.info(spy, TODAY, "$SPX") is None  # cash-settled: no assignment for dividends


def leg(pc, k, n, mark, exp="2026-12-18"):
    return {
        "kind": "OPTION",
        "put_call": pc,
        "strike": k,
        "quantity": n,
        "mark": mark,
        "expiration": exp,
    }


def test_at_risk_only_in_the_money_short_calls_with_little_time_value():
    div = {"ex_date": "2026-12-18", "amount": 1.83, "estimated": False, "act_by": "2026-12-17"}
    legs = [leg("C", 590, -1, 10.5), leg("P", 560, -1, 0.4)]
    # 600 spot: the 590 call is 10 in the money with 0.50 of time value, under 1.83.
    assert dv.at_risk(legs, 600, div, TODAY) == [{"strike": 590, "time_value": 0.5}]
    assert dv.at_risk([leg("C", 590, -1, 13.0)], 600, div, TODAY) == []  # 3.00 of time value
    assert dv.at_risk([leg("C", 610, -1, 1.0)], 600, div, TODAY) == []  # out of the money
    assert dv.at_risk([leg("C", 590, 1, 10.5)], 600, div, TODAY) == []  # long
    assert (
        dv.at_risk([leg("C", 590, -1, 10.5, "2026-12-11")], 600, div, TODAY) == []
    )  # expires first
    assert dv.at_risk(legs, 600, div, date(2026, 11, 20)) == []  # too far ahead to flag yet
    f = dv.flag(dv.at_risk(legs, 600, div, TODAY), div)
    assert (
        f["kind"] == "dividend" and f["label"] == "Ex-div 12-18" and "by 2026-12-17" in f["detail"]
    )


def test_automanage_rule_acts_on_the_flag():
    s = merged_settings({})
    assert s["levels"]["dividend"] == "notify"
    st = strat(flags=["dividend"])
    st["flags"][0]["act_by"] = "2026-12-17"
    acts = decide(st, 600, Rules(), s, ALL)
    assert ("dividend", "close") in kinds(acts)
    assert "close by 2026-12-17" in next(a.reason for a in acts if a.rule == "dividend")


def test_positions_flag_a_short_call_at_risk():
    """The snapshot carries the underlying's next ex-date and flags the short call."""
    from datetime import datetime

    from app.options import Leg
    from app.portfolio import PortfolioService

    call = "SPY   261218C00590000"
    p = PortfolioService(
        None, None, None, dict, None, "live", clock=lambda: datetime(2026, 12, 10, 11)
    )
    p.legs = [
        Leg(
            symbol=call,
            underlying="SPY",
            kind="OPTION",
            quantity=-1,
            avg_price=4.0,
            put_call="C",
            strike=590,
            expiration=date(2026, 12, 18),
            multiplier=100,
        )
    ]
    p.quotes = {
        "SPY": {
            "quote": {"mark": 600.0},
            "fundamental": {"nextDivExDate": "2026-12-18", "divPayAmount": 1.83},
        },
        call: {"quote": {"mark": 10.5, "delta": 0.8}},
    }
    p._build_and_publish()
    (u,) = p.snapshot["underlyings"]
    assert u["dividend"]["ex_date"] == "2026-12-18"
    assert [f["kind"] for f in u["strategies"][0]["flags"]].count("dividend") == 1
