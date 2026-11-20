from datetime import UTC, date, datetime, timedelta

import pytest

from app import analytics
from app.notify import ET, due_reminders
from app.options import Leg, bs, leg_from_schwab_position, option_symbol, parse_option_symbol
from app.schwab_service import ConnectionStatus
from app.strategies import Rules, evaluate, group, not_tasty

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
    # A short put covered by short shares or a put ZEBRA: nothing more; calls are not covered.
    put = [TradeLeg("P", 220, exp, -1, 2.50)]
    assert bp_estimate(put, 230, -21_750, held_short=100) == 0
    assert bp_estimate(put, 230, -21_750, held_shares=100) > 0
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
    profit = next(f for f in cc.flags if f["kind"] == "profit")  # the call is 55% done
    assert profit["label"] == "55% credit" and "of the call's credit" in profit["detail"]
    # Calendar: on the debit paid, 10% by default.
    cal = group([opt("P", 580, -1, 3.0, mark=2.5, exp=NEAR), opt("P", 580, 1, 5.0, mark=4.75)])[0]
    evaluate(cal, TODAY, 590, Rules())
    assert cal.name == "Calendar" and any(f["kind"] == "profit" for f in cal.flags)  # 25 of 200
    assert next(f for f in cal.flags if f["kind"] == "profit")["label"].endswith("% debit")
    # Debit verticals and long options are never flagged for profit.
    dv = group([opt("P", 580, 1, 3.0, mark=4.5), opt("P", 575, -1, 1.0, mark=1.5)])[0]
    evaluate(dv, TODAY, 570, Rules())
    assert not any(f["kind"] == "profit" for f in dv.flags)


def test_zebras_are_recognised_and_left_to_run():
    # Two long calls deeper in the money than one short at the money: a call ZEBRA.
    z = group(
        [
            opt("C", 570, 2, 22.0, mark=23.0, delta=0.75),
            opt("C", 590, -1, 9.0, mark=9.5, delta=-0.5),
        ]
    )
    assert [x.name for x in z] == ["Call ZEBRA"] and z[0].defined_risk
    evaluate(z[0], TODAY, 591.0, Rules())
    n = z[0].numbers
    assert n["open_value"] == -3500.0 and n["max_loss"] == 3500.0 and n["max_profit"] is None
    # A stock replacement (tastylive's Liz and Jenny): like 100 shares, not a non-tasty trade.
    assert n["shares_like"] == 100 and not not_tasty("Call ZEBRA", -3500.0)
    # The short is at the money by design: never "tested", no loss or profit flag.
    assert {f["kind"] for f in z[0].flags} == set()
    pz = group([opt("P", 610, 4, 20.0), opt("P", 590, -2, 8.0)])
    assert [(x.name, sorted(abs(leg.quantity) for leg in x.legs)) for x in pz] == [
        ("Put ZEBRA", [2, 4])
    ]
    evaluate(pz[0], TODAY, 589.0, Rules())
    assert pz[0].numbers["shares_like"] == -200
    # Not a ZEBRA: longs further out of the money (a back ratio), or one-for-one (a vertical).
    assert group([opt("C", 600, 2, 3.0), opt("C", 590, -1, 8.0)])[0].name == "Call back ratio"
    assert group([opt("C", 570, 1, 22.0), opt("C", 590, -1, 9.0)])[0].name == "Call vertical"
    # Extra longs beyond two per short stay as long calls.
    extra = group([opt("C", 570, 3, 22.0), opt("C", 590, -1, 9.0)])
    assert names(extra) == [("Call ZEBRA", 1), ("Long call", 1)]


def test_calls_written_against_a_zebra_are_covered_calls():
    from app.strategies import cover_calls, free_cover

    # One order: the call further out of the money is written against the ZEBRA.
    legs = [opt("C", 570, 2, 22.0), opt("C", 590, -1, 9.0), opt("C", 600, -1, 4.0)]
    got = {st.name: st for st in group(legs)}
    assert set(got) == {"Call ZEBRA", "Covered call"}
    assert sorted(x.strike for x in got["Call ZEBRA"].legs) == [570, 590]
    assert got["Covered call"].meta["covered_by"] == "ZEBRA"

    # Separate orders (separate trades): grouped apart, then covered across them.
    zebra = group([opt("C", 570, 2, 22.0), opt("C", 590, -1, 9.0)])
    near = group([opt("C", 600, -1, 2.0, exp=NEAR)])
    assert near[0].name == "Short call"
    both = zebra + near
    cover_calls(both)
    assert near[0].name == "Covered call" and near[0].meta["covered_by"] == "ZEBRA"
    cover_calls(both)  # again: unchanged
    assert near[0].name == "Covered call"
    evaluate(near[0], TODAY, 591.0, Rules())
    assert near[0].numbers["covered_by"] == "ZEBRA"
    assert near[0].numbers["option_open_value"] == 200
    assert free_cover(both, NEAR) == 0
    assert free_cover(zebra, NEAR) == 100

    # A call expiring after the ZEBRA is not covered by it.
    later = group([opt("C", 600, -1, 2.0, exp=date(2027, 1, 15))])
    lst = zebra + later
    cover_calls(lst)
    assert later[0].name == "Short call"
    assert free_cover(zebra, date(2027, 1, 15)) == 0

    # A put ZEBRA covers no calls; one ZEBRA unit does not cover two calls.
    pz = group([opt("P", 610, 2, 20.0), opt("P", 590, -1, 8.0)])
    lone = group([opt("C", 600, -1, 2.0, exp=NEAR)])
    cover_calls(pz + lone)
    assert lone[0].name == "Short call"
    two = group([opt("C", 600, -2, 2.0, exp=NEAR)])
    cover_calls(zebra + two)
    assert two[0].name == "Short call"

    # Selling the ZEBRA leaves the call naked again.
    cover_calls(near)
    assert near[0].name == "Short call" and "covered_by" not in near[0].meta


def test_calls_written_later_against_shares_are_covered_calls():
    from app.strategies import cover_calls

    shares = group([stock(100, 180.0, 185.0)])
    call = group([opt("C", 200, -1, 2.0, under="AAPL")])
    lst = shares + call
    cover_calls(lst)
    assert call[0].name == "Covered call" and call[0].meta["covered_by"] == "shares"
    few = group([stock(50, 180.0, 185.0)])
    call2 = group([opt("C", 200, -1, 2.0, under="AAPL")])
    cover_calls(few + call2)
    assert call2[0].name == "Short call"


def test_puts_written_against_a_put_zebra_are_covered_puts():
    from app.strategies import cover_calls, free_cover

    pz = group([opt("P", 610, 2, 20.0), opt("P", 590, -1, 8.0)])
    put = group([opt("P", 580, -1, 2.0, exp=NEAR)])
    assert put[0].name == "Short put"
    lst = pz + put
    cover_calls(lst)
    assert put[0].name == "Covered put" and put[0].meta["covered_by"] == "ZEBRA"
    assert put[0].meta["cover_until"] == EXP
    evaluate(put[0], TODAY, 589.0, Rules())
    n = put[0].numbers
    assert n["cover_until"] == EXP.isoformat() and n["option_open_value"] == 200
    assert free_cover(lst, NEAR, "P") == 0 and free_cover(pz, NEAR, "P") == 100
    # A call ZEBRA covers no puts, and a put past the ZEBRA's expiration is not covered.
    cz = group([opt("C", 570, 2, 22.0), opt("C", 590, -1, 9.0)])
    lone = group([opt("P", 580, -1, 2.0, exp=NEAR)])
    cover_calls(cz + lone)
    assert lone[0].name == "Short put"
    late = group([opt("P", 580, -1, 2.0, exp=date(2027, 1, 15))])
    cover_calls(pz + late)
    assert late[0].name == "Short put"
    # A covered option is not closed by the loss rule: what backs it offsets the loss.
    hurt = group([opt("P", 600, -1, 2.0, mark=12.0, delta=-0.7, exp=NEAR)])
    cover_calls(pz + hurt)
    evaluate(hurt[0], TODAY, 585.0, Rules())
    assert hurt[0].name == "Covered put" and "loss" not in {f["kind"] for f in hurt[0].flags}


def test_a_manual_roll_past_the_zebra_is_warned_about():
    from app.strategies import cover_calls, past_cover

    jan = date(2027, 1, 15)
    zebra = group([opt("C", 570, 2, 22.0), opt("C", 590, -1, 9.0)])
    call = group([opt("C", 600, -1, 2.0, exp=NEAR)])
    lst = zebra + call
    cover_calls(lst)
    sym = call[0].legs[0].symbol
    warn = past_cover(lst, {sym}, [("C", jan, -1)])
    assert warn and "covered call" in warn and EXP.isoformat() in warn
    assert past_cover(lst, {sym}, [("C", EXP, -1)]) is None
    # A covered put the same way.
    pz = group([opt("P", 610, 2, 20.0), opt("P", 590, -1, 8.0)])
    put = group([opt("P", 580, -1, 2.0, exp=NEAR)])
    plist = pz + put
    cover_calls(plist)
    assert "covered put" in past_cover(plist, {put[0].legs[0].symbol}, [("P", jan, -1)])
    # Covered by shares: shares do not expire, so no warning.
    shares = group([stock(100, 180.0, 185.0)])
    aapl = group([opt("C", 200, -1, 2.0, under="AAPL", exp=NEAR)])
    alist = shares + aapl
    cover_calls(alist)
    assert past_cover(alist, {aapl[0].legs[0].symbol}, [("C", jan, -1)]) is None


def test_butterflies_and_broken_wings_are_one_defined_risk_trade():
    fly = [
        opt("C", 590, 1, 6.0, mark=10.0),
        opt("C", 600, -2, 3.0, mark=4.0),
        opt("C", 610, 1, 1.0, mark=1.5),
    ]
    (st,) = group(fly)
    assert st.name == "Call butterfly" and st.defined_risk
    evaluate(st, TODAY, 600.0, Rules())
    n = st.numbers
    # Bought for 1.00: makes at most the 10-wide wing less the debit, loses at most the debit.
    assert n["open_value"] == -100.0 and n["max_profit"] == 900.0 and n["max_loss"] == 100.0
    # Worth 3.50 now: 250 of 900, past the 25% target.
    assert n["pl"] == 250.0 and n["pct_max_profit"] == pytest.approx(0.2778, abs=1e-4)
    assert "profit" in {f["kind"] for f in st.flags}

    bwb = [
        opt("P", 590, 1, 6.0),
        opt("P", 580, -2, 3.5),
        opt("P", 560, 1, 0.5),
    ]
    (st,) = group(bwb)
    assert st.name == "Put broken-wing butterfly" and st.defined_risk
    evaluate(st, TODAY, 600.0, Rules())
    n = st.numbers
    # Opened for a 0.50 credit: nothing to lose above 590; the 10 extra points of the wider
    # wing, less the credit, below 560.
    assert n["open_value"] == 50.0 and n["max_profit"] == 1050.0 and n["max_loss"] == 950.0

    # Two of each, and a short body that is not a butterfly's stays a ratio.
    (two,) = group([opt("C", 590, 2, 6.0), opt("C", 600, -4, 3.0), opt("C", 610, 2, 1.0)])
    assert two.name == "Call butterfly" and [x.quantity for x in two.legs] == [2, -4, 2]
    names = [s.name for s in group([opt("C", 590, 1, 6.0), opt("C", 600, -2, 3.0)])]
    assert names == ["Call ratio 1×2"]


# More tastylive structures -----------------------------------------------------------------


def test_lizards_super_bulls_back_ratios_double_calendars_and_collars_are_recognised():
    far = date(2027, 1, 15)
    cases = [
        ([opt("P", 590, -1, 8), opt("C", 590, -1, 7), opt("C", 600, 1, 3)], "Big lizard"),
        ([opt("P", 590, -1, 8), opt("C", 590, -1, 7), opt("P", 580, 1, 4)], "Reverse big lizard"),
        ([opt("C", 610, -1, 3), opt("P", 570, -1, 4), opt("P", 565, 1, 3)], "Reverse jade lizard"),
        ([opt("P", 570, -1, 4), opt("C", 590, 1, 9), opt("C", 600, -1, 5)], "Super bull"),
        ([opt("C", 610, -1, 4), opt("P", 590, 1, 9), opt("P", 580, -1, 5)], "Super bear"),
        (
            [opt("C", 615, 1, 2), opt("C", 610, -1, 4), opt("P", 590, 1, 9), opt("P", 580, -1, 5)],
            "Delta buster",
        ),
        ([opt("C", 600, -1, 5), opt("C", 610, 2, 2)], "Call back ratio"),
        ([opt("P", 580, -1, 5), opt("P", 570, 2, 2)], "Put back ratio"),
        (
            [opt("P", 570, -1, 3), opt("P", 570, 1, 5, exp=far)]
            + [opt("C", 610, -1, 3), opt("C", 610, 1, 5, exp=far)],
            "Double calendar",
        ),
        (
            [opt("P", 570, -1, 3), opt("P", 565, 1, 4, exp=far)]
            + [opt("C", 610, -1, 3), opt("C", 615, 1, 4, exp=far)],
            "Double diagonal",
        ),
        (
            [stock(100, 590, 590, "SPY"), opt("P", 570, 1, 4), opt("C", 610, -1, 5)],
            "Collar",
        ),
    ]
    for legs, name in cases:
        assert [x.name for x in group(legs)] == [name], name
    # Not mistaken for them: a long iron condor is two verticals; a modified super bull with
    # its long put is still one trade.
    assert sorted(
        names(
            group(
                [
                    opt("P", 565, -1, 1),
                    opt("P", 570, 1, 2),
                    opt("C", 610, 1, 2),
                    opt("C", 615, -1, 1),
                ]
            )
        )
    ) == [("Call vertical", 1), ("Put vertical", 1)]
    mod = group(
        [opt("P", 565, 1, 2), opt("P", 570, -1, 4), opt("C", 590, 1, 9), opt("C", 600, -1, 5)]
    )
    assert [(x.name, x.defined_risk) for x in mod] == [("Super bull", True)]


def test_numbers_and_targets_of_the_new_structures():
    r = Rules()
    # Big lizard: 12.00 credit against a 10-wide call spread, no upside risk; target 25%.
    bl = group(
        [
            opt("P", 590, -1, 8, mark=4),
            opt("C", 590, -1, 7, mark=4),
            opt("C", 600, 1, 3, mark=1),
        ]
    )[0]
    evaluate(bl, TODAY, 590.0, r)
    assert bl.numbers["max_profit"] == 1200.0 and bl.numbers["upside_risk"] == 0
    assert bl.numbers["pl"] == 500.0 and bl.numbers["pct_max_profit"] == round(500 / 1200, 4)
    assert any(f["kind"] == "profit" for f in bl.flags)  # past 25%
    # Reverse jade lizard: 4.00 credit against a 5-wide put spread: 1.00 of downside risk.
    rj = group([opt("C", 610, -1, 2), opt("P", 570, -1, 3), opt("P", 565, 1, 1)])[0]
    evaluate(rj, TODAY, 590.0, r)
    assert rj.numbers["downside_risk"] == 100.0 and rj.numbers["upside_risk"] is None
    # Super bull: 10-wide call spread for 4.00, paid by a 5.00 short put: 1.00 credit.
    sb = group(
        [opt("P", 570, -1, 5, mark=1), opt("C", 590, 1, 9, mark=12), opt("C", 600, -1, 5, mark=6)]
    )[0]
    evaluate(sb, TODAY, 600.0, r)
    assert sb.numbers["max_profit"] == 1100.0 and sb.numbers["max_loss"] is None
    # Well past 50% of max, yet no target (tastylive gives none) and no loss rule.
    assert sb.numbers["pct_max_profit"] > 0.5
    assert not {"profit", "loss"} & {f["kind"] for f in sb.flags}
    # With its long put (modified): the loss is the put spread less the credit.
    msb = group(
        [opt("P", 565, 1, 1), opt("P", 570, -1, 5), opt("C", 590, 1, 9), opt("C", 600, -1, 5)]
    )[0]
    evaluate(msb, TODAY, 590.0, r)
    assert msb.numbers["max_loss"] == 500.0 - 0.0  # 5 wide, opened for 0.00
    # Delta buster: a 10-wide put debit spread for 4.00, a 5-wide call credit spread for 2.00:
    # 2.00 debit. Most at or below 580; the call spread caps the loss.
    db = group(
        [opt("C", 615, 1, 2), opt("C", 610, -1, 4), opt("P", 590, 1, 9), opt("P", 580, -1, 5)]
    )[0]
    evaluate(db, TODAY, 590.0, r)
    assert db.defined_risk and db.numbers["open_value"] == -200.0
    assert db.numbers["max_profit"] == 800.0 and db.numbers["max_loss"] == 700.0
    # Call back ratio: sell the 600 for 5.00, buy two 610s for 2.00: 1.00 credit; worst at 610.
    br = group([opt("C", 600, -1, 5), opt("C", 610, 2, 2)])[0]
    evaluate(br, TODAY, 590.0, r)
    assert br.numbers["max_loss"] == 900.0 and br.numbers["max_profit"] is None
    # Put back ratio: sell the 580 for 5.00, buy two 570s for 2.00; most if it goes to zero.
    pb = group([opt("P", 580, -1, 5), opt("P", 570, 2, 2)])[0]
    evaluate(pb, TODAY, 590.0, r)
    assert pb.numbers["max_loss"] == 900.0 and pb.numbers["max_profit"] == 56100.0
    # Double diagonal: 2.00 debit (4.00 paid, 6.00 in... per side), longs 5 further out.
    dd = group(
        [
            opt("P", 570, -1, 3, mark=3),
            opt("P", 565, 1, 4, mark=4.4, exp=date(2027, 1, 15)),
            opt("C", 610, -1, 3, mark=3),
            opt("C", 615, 1, 4, mark=4.4, exp=date(2027, 1, 15)),
        ]
    )[0]
    evaluate(dd, TODAY, 590.0, r)
    assert dd.numbers["open_value"] == -200.0 and dd.numbers["max_loss"] == 700.0
    assert dd.numbers["pct_max_profit"] == 0.4 and any(f["kind"] == "profit" for f in dd.flags)
    # Collar: 100 shares at 590, 570 put for 4.00, 610 call for 5.00. Most at 610, least at 570.
    co = group(
        [
            stock(100, 590, 595, "SPY"),
            opt("P", 570, 1, 4, mark=2),
            opt("C", 610, -1, 5, mark=2),
        ]
    )[0]
    evaluate(co, TODAY, 595.0, r)
    assert co.numbers["max_profit"] == 2100.0 and co.numbers["max_loss"] == 1900.0
    # The target is on the call's credit alone (60% made), the put left out.
    assert co.numbers["option_open_value"] == 500.0 and co.numbers["pct_max_profit"] == 0.6
    assert any(f["kind"] == "profit" for f in co.flags)


def test_poor_mans_covered_call_targets_half_its_estimated_max_profit():
    from app.strategies import pmcc_extrinsic

    far = date(2027, 6, 18)
    long_c = opt("C", 500, 1, 98.0, mark=100.0, exp=far)
    long_c.iv = 0.20
    short_c = opt("C", 620, -1, 3.0, mark=2.0)
    st = group([long_c, short_c])[0]
    assert st.name == "Diagonal"
    evaluate(st, TODAY, 590.0, Rules())
    n = st.numbers
    ext = pmcc_extrinsic(long_c, short_c)
    assert ext > 0 and ext * 4 == int(ext * 4)  # rounded down to 0.25
    # The width less the 95.00 debit, plus the long's time value left.
    assert n["pmcc"] and n["open_value"] == -9500.0
    assert n["max_profit"] == round((120 + ext) * 100 - 9500, 2)
    assert n["pct_max_profit"] == round(n["pl"] / n["max_profit"], 4)
    # The target is 50% of that, not the calendar's 10% of the debit: not reached yet.
    assert not any(f["kind"] == "profit" for f in st.flags)
    long_c.mark = 140.0
    st = group([long_c, short_c])[0]
    evaluate(st, TODAY, 630.0, Rules())
    assert st.numbers["pct_max_profit"] >= 0.5
    assert any(f["detail"].startswith("Profit target 50% of the estimated max") for f in st.flags)
    # A call diagonal with the long above the short keeps the calendar rule.
    other = group([opt("C", 620, 1, 5.0, exp=far), opt("C", 600, -1, 3.0)])[0]
    evaluate(other, TODAY, 590.0, Rules())
    assert other.name == "Diagonal" and not other.numbers["pmcc"]
    # A poor man's covered put: the long put deeper in the money, further out.
    long_p = opt("P", 680, 1, 92.0, mark=92.0, exp=far)
    long_p.iv = 0.20
    short_p = opt("P", 560, -1, 3.0, mark=3.0)
    pm = group([long_p, short_p])[0]
    evaluate(pm, TODAY, 590.0, Rules())
    ext = pmcc_extrinsic(long_p, short_p)
    assert pm.numbers["pmcc"] and ext > 0
    assert pm.numbers["max_profit"] == round((120 + ext) * 100 - 8900, 2)
    # A put diagonal with the long below the short keeps the calendar rule.
    low = group([opt("P", 560, 1, 5.0, exp=far), opt("P", 580, -1, 3.0)])[0]
    evaluate(low, TODAY, 590.0, Rules())
    assert not low.numbers["pmcc"]


def test_an_inverted_strangle_and_iron_condor_say_so_with_what_they_can_make():
    # Rolled the put up past the call: 600 put, 590 call, 13.00 credit in all.
    (st,) = group([opt("P", 600, -1, 8.00, mark=12.0), opt("C", 590, -1, 5.00, mark=6.0)])
    assert st.name == "Strangle"
    evaluate(st, TODAY, 595.0, Rules())
    n = st.numbers
    assert n["inverted"] == 10 and n["max_profit"] == 300  # 1,300 credit less 1,000
    # An iron condor whose put side was rolled above the call side keeps its wings.
    legs = [
        opt("P", 590, 1, 1.00),
        opt("P", 600, -1, 6.00),
        opt("C", 595, -1, 4.00),
        opt("C", 605, 1, 1.00),
    ]
    (ic,) = group(legs)
    assert ic.name == "Iron condor"
    evaluate(ic, TODAY, 597.0, Rules())
    # Credit 800; between 595 and 600 it loses the 5 points of inversion; past a wing, 10.
    assert ic.numbers["inverted"] == 5
    assert ic.numbers["max_profit"] == 300 and ic.numbers["max_loss"] == 200
    # Not inverted: no flag, numbers as before.
    (ok,) = group([opt("P", 580, -1, 5.00), opt("C", 610, -1, 5.00)])
    evaluate(ok, TODAY, 595.0, Rules())
    assert ok.numbers["inverted"] is None and ok.numbers["max_profit"] == 1000
