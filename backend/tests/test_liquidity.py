from app.analytics import liquidity_stars
from app.chain import atm_liquidity


def test_stars_run_from_wide_and_thin_to_tight_and_deep():
    assert liquidity_stars(0.004, 0.03, 20_000) == 5  # a nickel or less, deep
    assert liquidity_stars(0.02, 0.20, 2_000) == 3  # fair spread, thousands
    assert liquidity_stars(0.08, 0.40, 200) == 1  # wide and thin
    # A penny-wide cheap option is tight even though it is a big share of its price.
    assert liquidity_stars(0.10, 0.02, 15_000) == 5


def test_one_missing_input_counts_the_other_twice():
    assert liquidity_stars(None, None, 20_000) == 5
    assert liquidity_stars(0.02, 0.2, None) == 3
    assert liquidity_stars(None, None, None) is None


def test_atm_liquidity_uses_the_strike_nearest_the_money():
    rows = [
        {"strike": 95, "call": {"bid": 6.0, "ask": 6.4, "mark": 6.2, "oi": 10}, "put": None},
        {
            "strike": 100,
            "call": {"bid": 3.0, "ask": 3.04, "mark": 3.02, "oi": 6000},
            "put": {"bid": 2.9, "ask": 2.96, "mark": 2.93, "oi": 5000},
        },
    ]
    liq = atm_liquidity(rows, 99.5)
    assert liq["oi"] == 11000 and liq["width"] == 0.05
    assert liquidity_stars(liq["spread_pct"], liq["width"], liq["oi"]) == 5
