import math
from datetime import date, timedelta

import pytest

from app import extremes as ex
from app import scanner as sc

S = sc.merged({})


def closes(values):
    start = date(2025, 11, 1)
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def calm(n=250):
    """A gentle wave between 90 and 110, so the year's range is 90-110."""
    return [round(90 + 20 * (0.5 + 0.5 * math.sin(i / 15)), 2) for i in range(n)]


def test_near_the_52_week_low_is_a_down_extreme():
    c = closes(calm())
    r = ex.compute(c, 90.5, 0.1, None, S)
    assert r["range_pos"] < 0.05 and r["direction"] == "down"
    assert "52-week low" in r["reasons"][0]
    r = ex.compute(c, 109.8, 0.1, None, S)
    assert r["direction"] == "up" and "52-week high" in r["reasons"][0]
    mid = ex.compute(c, 100.0, 0.1, None, S)
    assert mid["direction"] is None and not mid["extreme"]


def test_move_against_the_expected_move_needs_iv():
    values = [100.0] * 30
    # Five sessions ago 100, now 94: ln(0.94) = -6.2%; 20 IV over 5 days is 2.8%: -2.2 sigma.
    r = ex.compute(closes(values), 94.0, -0.5, 20.0, {**S, "range_band": 0.0})
    assert r["move_z"] == pytest.approx(-2.2, abs=0.05) and r["direction"] == "down"
    assert "5-day move -2.2σ" in r["reasons"]
    # Without IV the measure is skipped, not guessed.
    assert ex.compute(closes(values), 94.0, -0.5, None, {**S, "range_band": 0.0})["move_z"] is None


def test_todays_move_against_the_average_and_mixed_votes():
    values = [100 + (0.5 if i % 2 else -0.5) for i in range(40)]  # ~1% a day
    r = ex.compute(closes(values), 103.0, 2.6, None, {**S, "range_band": 0.0})
    assert r["day_ratio"] > 2 and r["direction"] == "up"
    # Up today but near the 52-week low: one vote each way, so no direction.
    c = closes(calm())
    mixed = ex.compute(c, 90.5, 3.0, None, S)
    assert mixed["extreme"] and mixed["direction"] is None


def _rows():
    def side(sym, d):
        return {"symbol": sym, "delta": d, "bid": 1.0, "ask": 1.1, "mark": 1.05}

    out = []
    for k, pd, cd in (
        (570, -0.16, 0.86),
        (580, -0.25, 0.75),
        (600, -0.75, 0.25),
        (610, -0.84, 0.16),
    ):
        out.append({"strike": k, "put": side(f"P{k}", pd), "call": side(f"C{k}", cd)})
    return out


def test_tilted_strangles():
    legs, label = sc.build_trade(_rows(), True, S)
    assert label == "Strangle 570/610"
    legs, label = sc.build_trade(_rows(), True, S, "down")
    assert label == "Strangle 580/610"  # the put at 25 delta: a bullish lean
    legs, label = sc.build_trade(_rows(), True, S, "up")
    assert label == "Strangle 570/600"
