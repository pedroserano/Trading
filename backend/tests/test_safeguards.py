"""Automation safeguards: daily loss limit, sign-in hold, wide spreads, early closes."""

from datetime import date, datetime

import pytest
from test_automation import NOW, QUOTES, Harness, q, strat, sym

from app import automation as auto
from app.notify import ET


@pytest.fixture
def anyio_backend():
    return "asyncio"


LOSS = {"levels": {"loss": "auto", "profit": "off"}}


async def ready(h, flags=("loss",)):
    tid = await h.open_strangle()
    h.portfolio.snapshot["underlyings"][0]["strategies"] = [strat(tid=tid, flags=list(flags))]
    await h.svc.set_trade(tid, True)
    return tid


@pytest.mark.anyio
async def test_the_daily_loss_limit_stops_automation_until_tomorrow():
    h = Harness([], QUOTES, settings=LOSS)
    await ready(h)
    h.portfolio.snapshot["account"] = {"net_liq": 100_000}
    h.svc.state = None
    await h.svc.load()
    h.svc.state["day_start"] = {"day": NOW.date().isoformat(), "net_liq": 100_000}
    h.portfolio.snapshot["account"] = {"net_liq": 97_500}  # down 2.5%, over 2%
    assert await h.svc.tick() == ["held: daily loss limit"]
    assert h.svc.blocked().startswith("stopped for the day")
    assert any("Automation stopped for the day" in t for t, _ in h.notifier.sent)
    assert not [o for o in h.broker.state["orders"] if o["intent"] == "close"]
    # Still stopped on a later tick, even if net liq recovers.
    h.portfolio.snapshot["account"] = {"net_liq": 100_500}
    assert await h.svc.tick() == ["held: daily loss limit"]
    # A new day starts from that day's net liq.
    h.svc.clock = lambda: datetime(2026, 11, 12, 11, 0, tzinfo=ET)
    h.portfolio.snapshot["data"]["quotes_at"] = __import__("time").time()
    assert await h.svc.tick() == ["place loss"]


@pytest.mark.anyio
async def test_a_lapsing_sign_in_holds_automation():
    h = Harness([], QUOTES, settings=LOSS)
    await ready(h)
    h.portfolio.snapshot["connection"] = {"expires_in_days": 0.6}
    assert await h.svc.tick() == ["held: sign-in"]
    assert "sign-in" in h.svc.blocked()
    h.portfolio.snapshot["connection"] = {"expires_in_days": 6.9}
    assert await h.svc.tick() == ["place loss"]


@pytest.mark.anyio
async def test_a_wide_order_waits_and_is_tried_again():
    wide = {**QUOTES, sym("P", 555): q(1.0, 3.0), sym("C", 610): q(1.0, 3.0)}
    h = Harness([], wide, settings=LOSS)
    await ready(h)
    assert await h.svc.tick() == []
    assert any("Skipped for now" in x["text"] for x in h.svc.state["log"])
    assert h.svc.state["acted"] == {}  # not marked done: the next tick tries again
    h.quotes = QUOTES
    assert await h.svc.tick() == ["place loss"]


def test_early_closes_end_the_window_at_12_30():
    s = auto.merged_settings({})
    assert auto.early_close(date(2026, 11, 27))  # the day after Thanksgiving
    assert auto.early_close(date(2026, 12, 24)) and auto.early_close(date(2025, 7, 3))
    assert not auto.early_close(date(2026, 7, 3))  # July 4 2026 is a Saturday: a holiday
    assert not auto.early_close(date(2026, 11, 26))
    half = datetime(2026, 11, 27, 12, 45, tzinfo=ET)
    assert not auto.in_window(half, s)
    assert auto.in_window(half.replace(hour=12, minute=15), s)
    assert auto.in_window(datetime(2026, 11, 30, 12, 45, tzinfo=ET), s)


def test_safeguard_settings_from_the_form():
    s = auto.parse_settings({"daily_loss_limit": "0", "max_order_spread": "40"}, None)
    assert s["daily_loss_limit"] == 0 and s["max_order_spread"] == 0.40


@pytest.mark.anyio
async def test_the_scanner_only_proposes_while_automation_is_held():
    from test_scanner import harness

    scanner, sim_auto, broker = harness({"level_sim": "auto"})
    await sim_auto.load()
    sim_auto.state["paused"] = True
    r = await scanner.scan("sim")
    assert r["acted"] == ["propose SPY"] and not (broker.state or {}).get("orders")
