"""Scanner rules: earnings through expiration, probability of profit, ranking, one trade per
expiration, the daily buying-power cap and rescans."""

import time
from datetime import datetime

import pytest
from test_scanner import EXP, Earnings, harness

from app import scanner as sc
from app.notify import ET


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_no_single_stock_trade_holds_through_earnings():
    scanner, _, _ = harness()
    # 15 sessions away (outside the 7-session buffer) but before the Dec 18 expiration.
    scanner.earnings = Earnings({"AAPL": "2026-12-03"})
    r = await scanner.scan("sim", act=False)
    reasons = {x["symbol"]: x["reason"] for x in r["skipped"]}
    assert reasons["AAPL"].startswith(
        f"earnings 2026-12-03 fall before the {EXP.isoformat()} expiration"
    )
    scanner2, _, _ = harness({"avoid_through_earnings": False})
    scanner2.earnings = Earnings({"AAPL": "2026-12-03"})
    r = await scanner2.scan("sim", act=False)
    assert "AAPL" in [c["symbol"] for c in r["candidates"]]


def _with_entry(auto, pop):
    first = auto.submit

    async def submit(raw, place):
        out = await first(raw, place)
        if not place:
            out["entry"] = {"pop": pop}
        return out

    auto.submit = submit


@pytest.mark.anyio
async def test_a_low_probability_of_profit_is_skipped_and_ranking_has_a_score():
    scanner, auto, _ = harness()
    _with_entry(auto, 0.40)
    r = await scanner.scan("sim", act=False)
    assert r["candidates"] == []
    assert "probability of profit 40%, under 55%" in r["skipped"][0]["reason"]
    scanner, auto, _ = harness()
    _with_entry(auto, 0.70)
    (c,) = (await scanner.scan("sim", act=False))["candidates"]
    assert c["score"] == pytest.approx(0.6 + min(abs(c["mid"]) * 100 / 4000, 1) * 2 + 0.7, abs=1e-3)


@pytest.mark.anyio
async def test_one_trade_per_underlying_and_expiration():
    scanner, _, _ = harness({"max_per_underlying": 3})
    scanner.portfolios["sim"].snapshot["underlyings"] = [
        {
            "symbol": "SPY",
            "strategies": [{"trade_id": 1, "numbers": {"expiration": EXP.isoformat()}}],
        }
    ]
    r = await scanner.scan("sim", act=False)
    reasons = {x["symbol"]: x["reason"] for x in r["skipped"]}
    assert reasons["SPY"] == f"already a trade here in {EXP.isoformat()}"


@pytest.mark.anyio
async def test_the_daily_buying_power_cap_holds():
    # Each preview asks 4,000 of buying power; 3% of 100k allows none.
    scanner, auto, _ = harness({"max_bp_day": 0.03})
    r = await scanner.scan("sim")
    assert r["acted"] == [] and "buying power added today" in r["candidates"][0]["note"]
    scanner, auto, _ = harness({"max_bp_day": 0.05})
    r = await scanner.scan("sim")
    assert r["acted"] == ["propose SPY"] and r["bp_today"] == 4000.0


@pytest.mark.anyio
async def test_rescans_during_the_day_when_set():
    scanner, _, _ = harness({"scan_every": 15})
    scanner.clock = lambda: datetime(2026, 11, 11, 11, 0, tzinfo=ET)
    assert await scanner.tick() == ["sim"]
    assert await scanner.tick() == []  # less than 15 minutes since the last scan
    st = await scanner._state()
    st["sim"]["at"] = time.time() - 16 * 60
    await scanner.store.set_setting(sc.STATE_KEY, st)
    assert await scanner.tick() == ["sim"]
    # Not after automanage's window ends.
    st = await scanner._state()
    st["sim"]["at"] = time.time() - 16 * 60
    await scanner.store.set_setting(sc.STATE_KEY, st)
    scanner.clock = lambda: datetime(2026, 11, 11, 15, 40, tzinfo=ET)
    assert await scanner.tick() == []
    with pytest.raises(ValueError, match="at least 5 minutes"):
        sc.parse_form({"scan_every": "2"}, None)
