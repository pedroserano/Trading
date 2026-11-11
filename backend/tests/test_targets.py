"""Portfolio targets (delta bias band, theta band) and guts to wings near the money."""

import pytest
from test_automation import ALL, kinds, leg, make_tested_svc, opened, strat, sym
from test_scanner import harness

from app import targets
from app.automation import decide, merged_settings, parse_settings
from app.preferences import Preferences
from app.store import MemoryStore
from app.strategies import Rules


@pytest.fixture
def anyio_backend():
    return "asyncio"


# Guts to wings near the money ---------------------------------------------------------


def test_a_deeply_tested_straddle_buys_the_guts():
    s = merged_settings({})
    straddle = [leg("P", 590, -1, -0.22), leg("C", 590, -1, 0.78)]
    st = strat(name="Straddle", legs=straddle, flags=["tested"])
    assert ("tested", "guts_to_wings") in kinds(decide(st, 604, Rules(), s, ALL))
    # Not yet deep enough: the usual tested-side roll.
    mild = strat(
        name="Straddle", legs=[leg("P", 590, -1, -0.40), leg("C", 590, -1, 0.60)], flags=["tested"]
    )
    assert ("tested", "roll_untested") in kinds(decide(mild, 594, Rules(), s, ALL))
    off = {**s, "guts_near": False}
    assert ("tested", "roll_untested") in kinds(decide(st, 604, Rules(), off, ALL))


def test_a_narrow_strangle_counts_but_a_wide_one_does_not():
    s = merged_settings({})
    narrow = strat(legs=[leg("P", 588, -1, -0.25), leg("C", 593, -1, 0.75)], flags=["tested"])
    assert ("tested", "guts_to_wings") in kinds(decide(narrow, 600, Rules(), s, ALL))
    wide = strat(legs=[leg("P", 570, -1, -0.10), leg("C", 593, -1, 0.75)], flags=["tested"])
    assert ("tested", "roll_untested") in kinds(decide(wide, 600, Rules(), s, ALL))
    # A 5-point gap is under 1% of 600; with the gap at 0.5% it is not narrow.
    tight = {**s, "guts_near_gap": 0.005}
    assert ("tested", "roll_untested") in kinds(decide(narrow, 600, Rules(), tight, ALL))


@pytest.mark.anyio
async def test_straddle_guts_pay_up_to_the_in_the_money_value():
    svc = make_tested_svc()
    await svc.load()
    st = strat(name="Straddle", legs=[leg("P", 590, -1, -0.22), leg("C", 590, -1, 0.78)])
    base = {"account": "sim", "trade_id": 1, "duration": "DAY"}
    (b,) = await svc._guts_bodies(st, {"symbol": "SPY", "price": 596}, merged_settings({}), base)
    assert opened(b) == sorted([sym("P", 560), sym("C", 620)])
    assert b["_max_debit"] == 6.25  # 6 in the money plus 0.25


def test_guts_near_settings_from_the_form():
    s = parse_settings(
        {"_form": "automation", "guts_near_gap": "0.5", "guts_near_delta": "0.8"}, None
    )
    assert s["guts_near"] is False and s["guts_near_gap"] == 0.005 and s["guts_near_delta"] == 0.8
    s = parse_settings({"_form": "automation", "guts_near": "on"}, None)
    assert s["guts_near"] is True and s["guts_near_delta"] == 0.70


# Bands ---------------------------------------------------------------------------------


def test_bands_from_the_snapshot_totals():
    r = Rules()
    # 50 SPY deltas at 600 on 100k net liq: +30% of net liq, above a neutral band.
    b = targets.bands({"delta_bw": 50.0, "theta_pct_nl": 0.002}, {"net_liq": 100_000}, 600, r)
    assert b["delta"]["value"] == pytest.approx(0.30) and b["delta"]["state"] == "above"
    assert b["theta"]["state"] == "inside" and b["bias"] == "neutral"
    assert targets.lean(b) == "up"
    b = targets.bands({"delta_bw": None, "theta_pct_nl": None}, None, 600, r)
    assert b["delta"]["state"] is None and targets.lean(b) is None


def test_a_trade_that_moves_further_out_is_warned():
    r = Rules()
    b = targets.bands({"delta_bw": -10.0, "theta_pct_nl": 0.0029}, {"net_liq": 100_000}, 600, r)
    assert b["delta"]["state"] == "below"  # -6%
    worse = targets.after(b, -0.02, 0.0005)
    assert worse["delta_push"] is True and len(worse["warnings"]) == 2
    assert "below the -5% bias band" in worse["warnings"][0]
    assert "above the 0.30% theta band" in worse["warnings"][1]
    better = targets.after(b, 0.01, -0.0001)  # back toward the band
    assert better["delta_push"] is False and better["warnings"] == []
    assert targets.after(None, 0.1, 0.1)["warnings"] == []


@pytest.mark.anyio
async def test_a_bias_sets_its_band_even_when_the_form_sends_the_old_one():
    prefs = Preferences(MemoryStore(), None)
    old_band = {"delta_min": "-5", "delta_max": "5", "theta_min": "0.1", "theta_max": "0.3"}
    r = await prefs.set_rules({**old_band, "delta_bias": "bullish"})
    assert (r.delta_bias, r.delta_min, r.delta_max) == ("bullish", 0.0, 0.15)
    # The same bias again keeps a band you adjusted.
    r = await prefs.set_rules(
        {
            "delta_min": "-2",
            "delta_max": "20",
            "theta_min": "0.1",
            "theta_max": "0.3",
            "delta_bias": "bullish",
        }
    )
    assert (r.delta_min, r.delta_max) == (-0.02, 0.20)
    r = await prefs.set_rules({**old_band, "delta_bias": "bearish"})
    assert (r.delta_min, r.delta_max) == (-0.15, 0.0)
    r = await prefs.set_rules({**old_band, "delta_bias": "neutral"})
    assert (r.delta_min, r.delta_max) == (-0.05, 0.05)
    with pytest.raises(ValueError, match="least must not be above"):
        await prefs.set_rules({"delta_min": "10", "delta_max": "5"})
    with pytest.raises(ValueError, match="neutral, bullish or bearish"):
        await prefs.set_rules({"delta_bias": "sideways"})


# Scanner -------------------------------------------------------------------------------


def _bands(delta=0.0, theta=0.002, r=None):
    r = r or Rules()
    return {
        "bias": r.delta_bias,
        "delta": {
            "value": delta,
            "min": r.delta_min,
            "max": r.delta_max,
            "state": targets.state(delta, r.delta_min, r.delta_max),
        },
        "theta": {
            "value": theta,
            "min": r.theta_min,
            "max": r.theta_max,
            "state": targets.state(theta, r.theta_min, r.theta_max),
        },
    }


@pytest.mark.anyio
async def test_scanner_leans_back_toward_the_delta_band():
    scanner, auto, broker = harness()
    scanner.portfolios["sim"].snapshot["bands"] = _bands(delta=-0.09)
    r = await scanner.scan("sim", act=False)
    (c,) = r["candidates"]
    assert c["tilt"] == "down" and c["band_lean"] is True
    assert "tilted bullish: portfolio delta -9.0% of net liq, below" in c["label"]
    # Switched off, no lean.
    scanner2, _, _ = harness({"band_lean": False})
    scanner2.portfolios["sim"].snapshot["bands"] = _bands(delta=-0.09)
    (c,) = (await scanner2.scan("sim", act=False))["candidates"]
    assert c["tilt"] is None


@pytest.mark.anyio
async def test_scanner_stops_at_the_theta_maximum():
    scanner, auto, broker = harness()
    scanner.portfolios["sim"].snapshot["bands"] = _bands(theta=0.0031)
    r = await scanner.scan("sim")
    assert r["candidates"] == [] and r["acted"] == []
    assert all(
        "theta 0.31% of net liq is at the 0.30% maximum" in x["reason"] for x in r["skipped"]
    )


@pytest.mark.anyio
async def test_a_trade_pushing_delta_out_is_flagged_and_only_proposed():
    scanner, auto, broker = harness({"level_sim": "auto"})
    first = auto.submit

    async def submit(raw, place):
        out = await first(raw, place)
        if not place:
            out["band_after"] = {
                "delta": 0.07,
                "theta": 0.002,
                "delta_push": True,
                "warnings": [
                    "Beta-weighted delta would be +7% of net liq, above the +5% bias band"
                ],
            }
        return out

    auto.submit = submit
    r = await scanner.scan("sim")
    (c,) = r["candidates"]
    assert c["delta_push"].startswith("Beta-weighted delta would be +7%")
    assert "Warning: Beta-weighted" in c["label"]
    assert r["acted"] == ["propose SPY"] and not (broker.state or {}).get("orders")
