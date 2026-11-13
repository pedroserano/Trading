from datetime import UTC, date, datetime, timedelta

import pytest

from app import analytics
from app.notify import ET, due_reminders
from app.options import Leg, bs, leg_from_schwab_position, option_symbol, parse_option_symbol
from app.schwab_service import ConnectionStatus
from app.strategies import Rules, evaluate, group

TODAY = date(2026, 11, 11)
EXP = date(2026, 12, 18)  # 37 DTE
NEAR = date(2026, 11, 27)  # 16 DTE


def opt(pc, strike, qty, avg, mark=None, delta=None, theta=-0.01, exp=EXP, under="SPY"):
    return Leg(
        symbol=option_symbol(under, exp, pc, strike),
        underlying=under,
        kind="OPTION",
        quantity=qty,
        avg_price=avg,
        put_call=pc,
        strike=strike,
        expiration=exp,
        multiplier=100,
        mark=mark,
        delta=delta,
        theta=theta,
        vega=0.1,
    )


def stock(qty, avg, mark, under="AAPL"):
    return Leg(
        symbol=under, underlying=under, kind="EQUITY", quantity=qty, avg_price=avg, mark=mark
    )


# Symbols and positions ----------------------------------------------------------------


def test_option_symbol_roundtrip():
    sym = option_symbol("SPY", date(2026, 12, 18), "P", 552.5)
    assert sym == "SPY   261218P00552500"
    occ = parse_option_symbol(sym)
    assert (occ.underlying, occ.expiration, occ.put_call, occ.strike) == (
        "SPY",
        date(2026, 12, 18),
        "P",
        552.5,
    )
    assert parse_option_symbol("AAPL") is None


def test_leg_from_short_option_position():
    leg = leg_from_schwab_position(
        {
            "shortQuantity": 2,
            "longQuantity": 0,
            "averagePrice": 3.1,
            "marketValue": -480.0,
            "instrument": {
                "assetType": "OPTION",
                "symbol": "IWM   261204P00224000",
                "underlyingSymbol": "IWM",
                "putCall": "PUT",
            },
        }
    )
    assert (leg.quantity, leg.avg_price, leg.strike, leg.put_call, leg.multiplier) == (
        -2,
        3.1,
        224.0,
        "P",
        100,
    )
    assert leg.mark == pytest.approx(2.4)


def test_leg_prefers_side_specific_average_and_skips_unknown_assets():
    leg = leg_from_schwab_position(
        {
            "shortQuantity": 1,
            "averagePrice": 9.0,
            "averageShortPrice": 2.5,
            "instrument": {"assetType": "OPTION", "symbol": "SPY   261218C00610000"},
        }
    )
    assert leg.avg_price == 2.5 and leg.underlying == "SPY"
    assert (
        leg_from_schwab_position(
            {"longQuantity": 5, "instrument": {"assetType": "MUTUAL_FUND", "symbol": "X"}}
        )
        is None
    )
    assert (
        leg_from_schwab_position(
            {"longQuantity": 0, "instrument": {"assetType": "EQUITY", "symbol": "X"}}
        )
        is None
    )


# Grouping -------------------------------------------------------------------------------


def names(strategies):
    return sorted((s.name, sorted(abs(x.quantity) for x in s.legs)[0]) for s in strategies)


def test_groups_iron_condor_and_strangle():
    legs = [
        opt("P", 540, 1, 1.0),
        opt("P", 545, -1, 1.5),
        opt("C", 610, -1, 1.4),
        opt("C", 615, 1, 0.9),
        opt("P", 520, -1, 2.0, exp=NEAR),
        opt("C", 630, -1, 1.0, exp=NEAR),
    ]
    assert names(group(legs)) == [("Iron condor", 1), ("Strangle", 1)]


def test_uneven_quantities_split_into_strangle_and_extra_short_put():
    legs = [opt("P", 550, -3, 3.0), opt("C", 610, -1, 2.0)]
    out = group(legs)
    assert names(out) == [("Short put", 2), ("Strangle", 1)]
    # Nothing lost or invented.
    assert sum(x.quantity for s in out for x in s.legs if x.put_call == "P") == -3


def test_groups_vertical_straddle_calendar_covered_call():
    legs = [
        opt("P", 575, -2, 3.4, exp=NEAR),
        opt("P", 570, 2, 2.35, exp=NEAR),
        opt("P", 590, -1, 8.0),
        opt("C", 590, -1, 7.5),
    ]
    assert names(group(legs)) == [("Put vertical", 2), ("Straddle", 1)]
    cal = group([opt("C", 600, -1, 2.0, exp=NEAR), opt("C", 600, 1, 4.0)])
    assert names(cal) == [("Calendar", 1)]
    diag = group([opt("P", 560, -1, 2.0, exp=NEAR), opt("P", 550, 1, 4.0)])
    assert names(diag) == [("Diagonal", 1)]
    cc = group([stock(250, 210, 230), opt("C", 240, -2, 3.2, under="AAPL")])
    assert names(cc) == [("Covered call", 2), ("Stock", 50)]
    assert names(group([opt("C", 650, 3, 1.0)])) == [("Long call", 3)]


# Numbers and flags ----------------------------------------------------------------------


def test_credit_vertical_numbers_and_dte_flag():
    legs = [
        opt("P", 575, -2, 3.40, mark=1.10, delta=-0.18, exp=NEAR),
        opt("P", 570, 2, 2.35, mark=0.60, delta=-0.12, exp=NEAR),
    ]
    (st,) = group(legs)
    evaluate(st, TODAY, 590.0, Rules())
    n = st.numbers
    assert n["open_value"] == 210.0  # (3.40 - 2.35) x 2 x 100
    assert n["credit"] is True
    assert n["pl"] == 110.0  # 210 - (1.10 - 0.60) x 200
    assert n["max_profit"] == 210.0 and n["max_loss"] == 790.0
    assert n["pct_max_profit"] == pytest.approx(0.5238, abs=1e-4)
    assert n["dte"] == 16
    assert n["delta"] == pytest.approx(12.0)  # (-2 x -0.18 + 2 x -0.12) x 100
    assert {f["kind"] for f in st.flags} == {"profit", "dte"}


def test_strangle_tested_and_loss_flags():
    legs = [
        opt("P", 555, -1, 4.85, mark=14.0, delta=-0.45),
        opt("C", 610, -1, 3.95, mark=0.30, delta=0.05),
    ]
    (st,) = group(legs)
    evaluate(st, TODAY, 552.0, Rules())
    assert st.numbers["pl"] == -550.0
    assert st.numbers["max_loss"] is None
    kinds = {f["kind"]: f for f in st.flags}
    assert kinds["tested"]["label"] == "ITM"
    assert "loss" not in kinds  # -550 is not yet 2x the 880 credit
    legs[0].mark = 27.0  # loss 1850 against 880 credit
    (st,) = group(legs)
    evaluate(st, TODAY, 540.0, Rules())
    assert "loss" in {f["kind"] for f in st.flags}


def test_iron_condor_max_loss_uses_wider_side():
    legs = [
        opt("P", 465, 1, 1.5, 1.5, -0.05),
        opt("P", 475, -1, 2.75, 2.75, -0.15),
        opt("C", 540, -1, 2.30, 2.30, 0.15),
        opt("C", 545, 1, 1.60, 1.60, 0.10),
    ]
    (st,) = group(legs)
    evaluate(st, TODAY, 510.0, Rules())
    assert st.numbers["open_value"] == 195.0
    assert st.numbers["max_loss"] == 805.0  # 10-wide put side
    assert st.flags == []


def test_missing_quote_blanks_totals_and_flags():
    (st,) = group([opt("P", 550, -1, 3.0, mark=None, delta=None)])
    evaluate(st, TODAY, 590.0, Rules())
    assert st.numbers["pl"] is None and st.numbers["delta"] is None
    assert [f["kind"] for f in st.flags] == ["data"]


def test_straddle_uses_lower_profit_target():
    legs = [
        opt("P", 590, -1, 8.0, mark=6.0, delta=-0.5),
        opt("C", 590, -1, 7.5, mark=5.5, delta=0.5),
    ]
    (st,) = group(legs)
    evaluate(st, TODAY, 590.0, Rules())
    assert st.numbers["pct_max_profit"] == pytest.approx(0.2581, abs=1e-4)
    assert "profit" in {f["kind"] for f in st.flags}


# Analytics ------------------------------------------------------------------------------


def test_iv30_interpolates_total_variance():
    assert analytics.iv30([(23, 14.0), (37, 16.0)]) == pytest.approx(15.26, abs=0.01)
    assert analytics.iv30([(44, 18.0)]) == 18.0
    assert analytics.iv30([]) is None


def test_atm_iv_from_chain_ignores_missing_values():
    chain = {
        "underlyingPrice": 591.0,
        "callExpDateMap": {
            "2026-12-11:30": {
                "590.0": [{"volatility": 15.0, "daysToExpiration": 30}],
                "595.0": [{"volatility": 14.0, "daysToExpiration": 30}],
            }
        },
        "putExpDateMap": {
            "2026-12-11:30": {"590.0": [{"volatility": -999.0, "daysToExpiration": 30}]}
        },
    }
    assert analytics.atm_iv_by_expiration(chain) == [(30, 15.0)]


def test_iv_rank_and_percentile():
    r = analytics.iv_rank(20.0, [10.0, 15.0, 30.0, 25.0])
    assert r["rank"] == 50.0 and r["percentile"] == 50.0 and r["days"] == 4
    assert analytics.iv_rank(None, [1.0]) is None


def test_beta_of_scaled_returns():
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(100)]
    bench, stock_, b, s = [], [], 100.0, 50.0
    for i, d in enumerate(days):
        r = 0.01 if i % 3 else -0.012
        b *= 1 + r
        s *= 1 + 1.5 * r
        bench.append((d, b))
        stock_.append((d, s))
    assert analytics.beta(stock_, bench) == pytest.approx(1.5, abs=1e-6)
    assert analytics.beta(stock_[:10], bench[:10]) is None


@pytest.mark.parametrize("vix,share", [(12, 0.25), (15, 0.30), (25, 0.35), (35, 0.40), (55, 0.50)])
def test_bp_ceiling_tiers(vix, share):
    assert analytics.bp_ceiling(vix) == share


def test_black_scholes_sanity():
    c, p = bs("C", 100, 100, 0.25, 0.2), bs("P", 100, 100, 0.25, 0.2)
    # Put-call parity.
    assert c["price"] - p["price"] == pytest.approx(100 - 100 * 2.718281828 ** (-0.01), abs=1e-6)
    assert 0.5 < c["delta"] < 0.6 and -0.5 < p["delta"] < -0.4
    assert c["theta"] < 0 and c["vega"] > 0


# Reminders ------------------------------------------------------------------------------


def status(age_days, connected=True):
    return ConnectionStatus(connected=connected, token_age_s=age_days * 86400, created_at=1000)


def at(y, m, d, h):
    return datetime(y, m, d, h, tzinfo=ET).astimezone(UTC)


def test_weekly_reminder_on_chosen_day_and_hour_only():
    sunday_7pm, sunday_5pm = at(2026, 11, 15, 19), at(2026, 11, 15, 17)
    assert [k for k, *_ in due_reminders(sunday_7pm, status(3), 6, 18)] == ["weekly:2026-W46"]
    assert due_reminders(sunday_5pm, status(3), 6, 18) == []
    # Not right after a fresh sign-in.
    assert due_reminders(sunday_7pm, status(0.5), 6, 18) == []


def test_expiry_and_lost_reminders():
    monday = at(2026, 11, 16, 10)
    assert [k for k, *_ in due_reminders(monday, status(5.5), 6, 18)] == ["exp48:1000"]
    assert [k for k, *_ in due_reminders(monday, status(6.2), 6, 18)] == ["exp24:1000"]
    assert [k for k, *_ in due_reminders(monday, status(7.1, False), 6, 18)] == ["lost:1000"]
    assert due_reminders(monday, ConnectionStatus(connected=False), 6, 18) == []


def test_day_pl_from_yesterdays_close_and_todays_fills():
    from app.options import Leg, day_pl

    def short_put(qty, avg, mark, close=None, prev=None):
        leg = Leg("SPY   261218P00555000", "SPY", "OPTION", qty, avg, "P", 555.0, None, 100)
        leg.mark = mark
        if close is not None:
            leg.extra["close"] = close
        if prev is not None:
            leg.extra["prev_qty"] = prev
        return leg

    # Held since before today: from yesterday's close (3.00) to now (2.60) on one short.
    assert day_pl(short_put(-1, 4.00, 2.60, close=3.00, prev=-1)) == pytest.approx(40)
    # Opened today at 4.00: from the fill price.
    assert day_pl(short_put(-1, 4.00, 3.80, close=3.00, prev=0)) == pytest.approx(20)
    # One held, one added today (average 3.50 for both; the new one counts from it).
    assert day_pl(short_put(-2, 3.50, 3.20, close=3.00, prev=-1)) == pytest.approx(-20 + 30)
    # Held overnight but no close known: unknown, not wrong.
    assert day_pl(short_put(-1, 4.00, 2.60, prev=-1)) is None
    # Schwab did not say what was held: treated as all held overnight.
    assert day_pl(short_put(-1, 4.00, 2.60, close=3.00)) == pytest.approx(40)


def test_bp_for_ratios_and_covered_calls():
    from app.analysis import TradeLeg, bp_estimate

    exp = date(2026, 12, 18)
    front = [TradeLeg("P", 575, exp, 1, 5.93), TradeLeg("P", 555, exp, -2, 3.10)]
    # The long put covers one short (a debit spread, nothing more at risk); the other is naked:
    # 20% of 590 less 35 out of the money (83) plus its 3.10 premium, and the long's cost.
    assert bp_estimate(front, 590, -53_473) == pytest.approx((118 - 35 + 3.10) * 100 + 593)
    call = [TradeLeg("C", 240, exp, -1, 3.15)]
    assert bp_estimate(call, 230, float("-inf"), held_shares=100) == 0
    assert bp_estimate(call, 230, float("-inf")) == pytest.approx((46 - 10 + 3.15) * 100)
    condor = [
        TradeLeg("P", 550, exp, 1, 1.0),
        TradeLeg("P", 555, exp, -1, 1.5),
        TradeLeg("C", 620, exp, -1, 1.5),
        TradeLeg("C", 625, exp, 1, 1.0),
    ]
    assert bp_estimate(condor, 590, -400.0) == 400.0  # defined risk: the max loss


# Every tastylive trade ---------------------------------------------------------------------


def test_ratios_and_jade_lizards_are_recognised():
    ratio = group([opt("P", 580, 1, 6.0), opt("P", 575, -2, 3.25)])
    assert [n for n, _ in names(ratio)] == ["Put ratio 1×2"] and not ratio[0].defined_risk
    call_ratio = group([opt("C", 600, 1, 4.0), opt("C", 610, -2, 2.25)])
    assert [n for n, _ in names(call_ratio)] == ["Call ratio 1×2"]
    lizard = group([opt("P", 560, -1, 3.0), opt("C", 610, -1, 2.0), opt("C", 612, 1, 1.0)])
    assert [n for n, _ in names(lizard)] == ["Jade lizard"] and not lizard[0].defined_risk
    # Unchanged: a vertical is still a vertical, an iron condor still an iron condor.
    assert group([opt("P", 580, 1, 6.0), opt("P", 575, -1, 4.0)])[0].name == "Put vertical"
    ic = [opt("P", 550, 1, 1), opt("P", 555, -1, 2), opt("C", 610, -1, 2), opt("C", 615, 1, 1)]
    assert [x.name for x in group(ic)] == ["Iron condor"]


def test_ratio_profit_is_a_share_of_width_plus_credit():
    # 5 wide for a 0.50 credit: max profit 550 at the short strike; 10% of it is 55.
    (st,) = group([opt("P", 580, 1, 6.0, mark=5.0), opt("P", 575, -2, 3.25, mark=2.2)])
    evaluate(st, TODAY, 590, Rules())
    assert st.numbers["max_profit"] == 550
    assert st.numbers["pl"] == pytest.approx(110)  # 0.50 in, closes for +0.60
    assert any(f["kind"] == "profit" for f in st.flags)
    # The loss rule measures a ratio against what it can make, not its small credit.
    (bad,) = group([opt("P", 580, 1, 6.0, mark=9.0), opt("P", 575, -2, 3.25, mark=6.0)])
    evaluate(bad, TODAY, 570, Rules())
    assert not any(f["kind"] == "loss" for f in bad.flags)  # down 250, under 2x 550


def test_lizard_covered_call_and_calendar_profit_bases():
    lizard = group(
        [
            opt("P", 560, -1, 3.0, mark=1.4),
            opt("C", 610, -1, 2.0, mark=0.5),
            opt("C", 612, 1, 1.0, mark=0.2),
        ]
    )[0]
    evaluate(lizard, TODAY, 590, Rules())
    assert lizard.numbers["max_profit"] == 400 and lizard.numbers["upside_risk"] == 0
    assert any(f["kind"] == "profit" for f in lizard.flags)  # 230 of 400
    # Covered call: on the call's credit alone, whatever the shares do.
    cc = group([stock(100, 200.0, 180.0, "SPY"), opt("C", 210, -1, 2.0, mark=0.9)])[0]
    evaluate(cc, TODAY, 180, Rules())
    assert cc.name == "Covered call" and cc.numbers["option_open_value"] == 200
    assert any(f["kind"] == "profit" for f in cc.flags)  # the call is 55% done
    # Calendar: on the debit paid, 10% by default.
    cal = group([opt("P", 580, -1, 3.0, mark=2.5, exp=NEAR), opt("P", 580, 1, 5.0, mark=4.75)])[0]
    evaluate(cal, TODAY, 590, Rules())
    assert cal.name == "Calendar" and any(f["kind"] == "profit" for f in cal.flags)  # 25 of 200
    # Debit verticals and long options are never flagged for profit.
    dv = group([opt("P", 580, 1, 3.0, mark=4.5), opt("P", 575, -1, 1.0, mark=1.5)])[0]
    evaluate(dv, TODAY, 570, Rules())
    assert not any(f["kind"] == "profit" for f in dv.flags)
