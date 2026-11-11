from datetime import date, datetime

import httpx
import pytest

from app import earnings as er
from app.automation import decide, merged_settings
from app.notify import ET
from app.store import MemoryStore
from app.strategies import Rules

TODAY = date(2026, 11, 11)  # a Wednesday


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_before_the_open_means_out_the_session_before():
    amc = {"date": "2026-11-13", "time": "amc"}  # Friday after the close
    bmo = {"date": "2026-11-16", "time": "bmo"}  # Monday before the open
    assert er.last_session(amc) == date(2026, 11, 13)
    assert er.last_session(bmo) == date(2026, 11, 13)  # back over the weekend
    assert er.last_session({"date": "2026-11-16", "time": None}) == date(2026, 11, 13)
    assert er.sessions_until(TODAY, date(2026, 11, 13)) == 2
    assert er.sessions_until(TODAY, TODAY) == 0
    d = er.describe(bmo, TODAY)
    assert d["label"] == "before the open" and d["days"] == 5 and d["sessions_left"] == 2


def test_parsers_read_finnhub_and_your_dates():
    fin = {
        "earningsCalendar": [
            {"date": "2027-01-28", "hour": "amc", "symbol": "AAPL"},
            {"date": "2026-10-30", "hour": "amc", "symbol": "AAPL"},  # past: ignored
            {"date": "2026-12-04", "hour": "", "symbol": "ORCL"},
        ]
    }
    out = er.parse_finnhub(fin, TODAY)
    assert out["AAPL"] == {"date": "2027-01-28", "time": "amc", "source": "Finnhub"}
    assert out["ORCL"]["time"] is None
    manual, bad = er.parse_manual("aapl 2027-01-28 AMC\n# note\nTSLA 2027-01-21\nnope", TODAY)
    assert manual["AAPL"]["time"] == "amc" and manual["TSLA"]["time"] is None
    assert bad == ["nope"]


class Quotes:
    async def quotes(self, symbols):
        return {
            s: {"assetMainType": "EQUITY", "assetSubType": "ETF" if s == "SPY" else None}
            for s in symbols
        }


@pytest.mark.anyio
async def test_refresh_with_finnhub_and_your_dates_win():
    def handler(request: httpx.Request):
        assert request.url.host == "finnhub.io"
        assert request.headers["X-Finnhub-Token"] == "KEY" and "token" not in request.url.params
        sym = request.url.params["symbol"]
        return httpx.Response(
            200, json={"earningsCalendar": [{"date": "2026-11-19", "hour": "amc", "symbol": sym}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def symbols():
        return {"SPY", "NVDA", "$SPX"}

    clock = lambda: datetime(2026, 11, 11, 12, tzinfo=ET)  # noqa: E731
    fin = er.EarningsService(MemoryStore(), Quotes(), symbols, "KEY", clock, client)
    fin.pace = 0
    assert await fin.refresh() == 1
    assert fin.info("NVDA")["source"] == "Finnhub" and fin.info("SPY") is None
    assert fin.is_fund("SPY") and fin.is_fund("$SPX")
    # Without a key: only dates entered in Settings.
    manual = er.EarningsService(MemoryStore(), Quotes(), symbols, "", clock, client)
    assert await manual.refresh() == 0 and manual.info("NVDA") is None
    assert manual.provider == "manual"
    await manual.set_manual("NVDA 2026-11-20 bmo")
    assert manual.info("NVDA")["source"] == "you" and manual.info("NVDA")["date"] == "2026-11-20"
    await fin.set_manual("NVDA 2026-11-20 bmo")
    assert fin.info("NVDA")["source"] == "you"  # yours win over the source


def strat(exp="2026-12-18"):
    legs = [
        {
            "symbol": "X",
            "kind": "OPTION",
            "put_call": "P",
            "strike": 100,
            "expiration": exp,
            "quantity": -1,
            "delta": -0.16,
        },
    ]
    return {
        "trade_id": 1,
        "rolls": 0,
        "name": "Short put",
        "defined_risk": False,
        "flags": [],
        "legs": legs,
        "numbers": {"dte": 37, "open_value": 200, "contracts": 1, "expiration": exp},
    }


def test_automanage_closes_before_earnings_only_when_the_trade_holds_through_them():
    s = merged_settings({})
    lv = {r: "propose" for r in s["levels"]}
    soon = er.describe({"date": "2026-11-13", "time": "amc"}, TODAY)  # 2 sessions left
    acts = decide(strat(), 110, Rules(), s, lv, earnings=soon)
    assert not any(a.rule == "earnings" for a in acts)  # default: 1 session ahead
    tomorrow = er.describe({"date": "2026-11-13", "time": "bmo"}, TODAY)  # out by Thursday
    acts = decide(strat(), 110, Rules(), s, lv, earnings=tomorrow)
    assert acts[0].rule == "earnings" and acts[0].kind == "close"
    assert "before the open" in acts[0].reason
    # A report after the options expire does not matter.
    assert not any(
        a.rule == "earnings"
        for a in decide(strat("2026-11-12"), 110, Rules(), s, lv, earnings=tomorrow)
    )
    off = {**lv, "earnings": "off"}
    assert not any(
        a.rule == "earnings" for a in decide(strat(), 110, Rules(), s, off, earnings=tomorrow)
    )
