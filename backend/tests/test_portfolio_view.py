"""The Portfolio screen's numbers, from a Positions snapshot."""

from datetime import date

import pytest

from app import portfolio_view as pv

TODAY = date(2026, 11, 11)


def leg(pc, k, n, exp="2026-12-18", mark=1.0, kind="OPTION"):
    return {
        "symbol": f"X{pc}{k}",
        "kind": kind,
        "put_call": pc,
        "strike": k,
        "expiration": exp,
        "quantity": n,
        "avg_price": mark,
        "mark": mark,
    }


def strat(name, legs, delta, theta, exp, dte, defined=False, max_loss=None, flags=()):
    return {
        "trade_id": name,
        "name": name,
        "defined_risk": defined,
        "flags": [{"kind": f} for f in flags],
        "legs": legs,
        "numbers": {
            "delta": delta,
            "theta": theta,
            "expiration": exp,
            "dte": dte,
            "max_loss": max_loss,
        },
    }


def snap():
    spy = [
        strat("Strangle", [leg("P", 550, -1), leg("C", 640, -1)], -10.0, 20.0, "2026-12-18", 37),
        strat(
            "Put vertical",
            [leg("P", 570, -1, "2026-11-27"), leg("P", 565, 1, "2026-11-27")],
            8.0,
            4.0,
            "2026-11-27",
            16,
            defined=True,
            max_loss=350.0,
            flags=["dte"],
        ),
    ]
    xle = [strat("Short put", [leg("P", 80, -1)], 20.0, 3.0, "2026-12-18", 37)]
    return {
        "account": {"net_liq": 100_000, "bp_used_pct": 0.2, "bp_ceiling_pct": 0.3},
        "market": {"spy": 600.0},
        "bands": None,
        "totals": {},
        "underlyings": [
            {
                "symbol": "SPY",
                "price": 600.0,
                "beta": 1.0,
                "delta_bw": -2.0,
                "theta": 24.0,
                "strategies": spy,
            },
            {
                "symbol": "XLE",
                "price": 90.0,
                "beta": 0.8,
                "delta_bw": 2.4,
                "theta": 3.0,
                "strategies": xle,
            },
            {
                "symbol": "NVDA",
                "price": None,
                "beta": None,
                "delta_bw": None,
                "theta": None,
                "strategies": [],
            },
        ],
    }


def test_buying_power_by_underlying_and_sector():
    out = pv.build(snap(), TODAY, {"NVDA": "Semiconductors"})
    rows = {r["symbol"]: r for r in out["underlyings"]}
    # Defined risk: the max loss. Undefined: Reg T, the larger side (the call, 20% of 600 less
    # 40 out of the money, plus its premium) and the other side's premium.
    assert rows["SPY"]["bp"] == pytest.approx(350 + 8_100 + 100)
    assert rows["SPY"]["bp_pct"] == pytest.approx(0.0855)
    assert rows["XLE"]["sector"] == "Energy" and rows["NVDA"]["sector"] == "Semiconductors"
    assert rows["SPY"]["delta_pct"] == pytest.approx(-2.0 * 600 / 100_000)
    secs = {s["sector"]: s for s in out["sectors"]}
    assert secs["Broad index"]["symbols"] == ["SPY"] and secs["Energy"]["bp"] > 0
    assert pv.sector("ZZZZ") == pv.UNKNOWN


def test_delta_by_position_theta_ladder_and_the_21_dte_calendar():
    out = pv.build(snap(), TODAY)
    # Positions by size of beta-weighted delta: XLE 20 x 0.8 x 90 / 600 = 2.4.
    assert [p["name"] for p in out["positions"]] == ["Strangle", "Put vertical", "Short put"]
    assert out["positions"][2]["delta_bw"] == 2.4
    ladder = {r["expiration"]: r for r in out["ladder"]}
    assert ladder["2026-12-18"]["theta"] == 23.0 and ladder["2026-12-18"]["trades"] == 2
    weeks = {w["week"]: w for w in out["calendar"]}
    assert [t["name"] for t in weeks["now"]["trades"]] == ["Put vertical"]
    # Dec 18 minus 21 days is Nov 27, in the week of Monday Nov 23.
    assert {t["underlying"] for t in weeks["2026-11-23"]["trades"]} == {"SPY", "XLE"}
    assert out["calendar"][-1]["week"] == "later"
