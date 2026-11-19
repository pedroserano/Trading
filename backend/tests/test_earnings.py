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
        if request.url.path.endswith("/profile2"):
            return httpx.Response(200, json={"finnhubIndustry": "Semiconductors"})
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
    assert fin.industries == {"NVDA": "Semiconductors"}  # stocks only, for the Portfolio screen
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


def _nasdaq_handler(days: dict, seen: list):
    """days: ISO date -> rows ([(symbol, time)]); a date not listed has no calendar."""

    def handler(request: httpx.Request):
        assert request.url.host == "api.nasdaq.com"
        assert "Mozilla" in request.headers["User-Agent"]
        d = request.url.params["date"]
        seen.append(d)
        if d not in days:
            return httpx.Response(200, json={"data": None})
        rows = [{"symbol": s, "time": t} for s, t in days[d]]
        return httpx.Response(200, json={"data": {"rows": rows}})

    return handler


def test_parse_nasdaq():
    rows = {
        "data": {
            "rows": [
                {"symbol": "WMT", "time": "time-pre-market"},
                {"symbol": "intu", "time": "time-after-hours"},
                {"symbol": "BJ", "time": "time-not-supplied"},
            ]
        }
    }
    assert er.parse_nasdaq(rows) == {"WMT": "bmo", "INTU": "amc", "BJ": None}
    assert er.parse_nasdaq({"data": None}) is None
    assert er.parse_nasdaq({"data": {"rows": None}}) == {}


@pytest.mark.anyio
async def test_nasdaq_gives_coming_and_last_reports_and_reads_past_days_once():
    seen = []
    days = {
        "2026-11-12": [("WMT", "time-pre-market")],
        "2026-11-24": [("NVDA", "time-after-hours"), ("WMT", "time-pre-market")],
        "2026-10-29": [("AAPL", "time-after-hours")],
        "2026-10-15": [("NFLX", "time-after-hours")],
    }
    # Every weekday back has a calendar (empty unless listed); ahead, none past Nov 24.
    for d in er.weekdays(date(2026, 10, 2), date(2026, 11, 10)):
        days.setdefault(d.isoformat(), [])
    days.setdefault("2026-11-11", [])
    for d in er.weekdays(date(2026, 11, 12), date(2026, 11, 24)):
        days.setdefault(d.isoformat(), [])
    client = httpx.AsyncClient(transport=httpx.MockTransport(_nasdaq_handler(days, seen)))

    async def symbols():
        return {"WMT", "NVDA", "AAPL", "NFLX", "SPY"}

    clock = lambda: datetime(2026, 11, 11, 12, tzinfo=ET)  # noqa: E731
    store = MemoryStore()
    e = er.EarningsService(store, Quotes(), symbols, "", clock, client, nasdaq=True)
    e.pace = 0
    assert await e.refresh() == 2
    assert e.provider == "Nasdaq"
    assert e.info("WMT")["date"] == "2026-11-12" and e.info("WMT")["time"] == "bmo"
    assert e.info("NVDA")["date"] == "2026-11-24" and e.info("NVDA")["source"] == "Nasdaq"
    assert e.info("AAPL") is None and e.last_report("AAPL") == "2026-10-29"
    # Ahead stops after three days with no calendar.
    assert "2026-11-27" in seen and "2026-11-30" not in seen
    # Just reported: clear for 60 days after the report; beyond 30 days back, not.
    assert e.quiet_until("AAPL", 30, 60) == {"last": "2026-10-29", "until": "2026-12-28"}
    assert e.quiet_until("NFLX", 30, 60)["until"] == "2026-12-14"  # 27 days back
    assert e.quiet_until("NFLX", 20, 60) is None
    assert e.quiet_until("AAPL", 0, 60) is None  # off
    assert e.quiet_until("WMT", 30, 60) is None  # a coming date is known
    assert e.quiet_until("SPY", 30, 60) is None  # funds have no reports
    # Again: the weeks back are not read twice.
    back = [d for d in seen if d < "2026-11-11"]
    seen.clear()
    await e.refresh()
    assert back and not [d for d in seen if d < "2026-11-11"]
    # Kept across a restart.
    again = er.EarningsService(store, Quotes(), symbols, "", clock, client, nasdaq=True)
    await again.load()
    assert again.info("NVDA")["date"] == "2026-11-24" and again.last_report("AAPL") == "2026-10-29"


@pytest.mark.anyio
async def test_earliest_source_wins_and_yours_beat_both():
    clock = lambda: datetime(2026, 11, 11, 12, tzinfo=ET)  # noqa: E731

    async def symbols():
        return set()

    e = er.EarningsService(MemoryStore(), Quotes(), symbols, "", clock)
    e.nasdaq = {"X": {"date": "2026-12-03", "time": "amc", "source": "Nasdaq"}}
    e.cache = {"X": {"date": "2026-12-01", "time": None, "source": "Finnhub"}}
    assert e.info("X")["source"] == "Finnhub"  # earlier
    e.cache = {"X": {"date": "2026-12-10", "time": None, "source": "Finnhub"}}
    assert e.info("X")["source"] == "Nasdaq"
    await e.set_one("X", "2026-12-20", "bmo")
    await e.set_one("Y", "2026-12-21", None)
    assert e.info("X")["source"] == "you" and e.info("X")["time"] == "bmo"
    assert await e.manual_text() == "X 2026-12-20 bmo\nY 2026-12-21"
    await e.set_one("X", None, None)
    assert e.info("X")["source"] == "Nasdaq" and "X" not in e.manual
    with pytest.raises(ValueError):
        await e.set_one("X", "2026-13-01", None)


@pytest.mark.anyio
async def test_a_failing_nasdaq_keeps_what_it_had():
    def handler(request):
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def symbols():
        return {"NVDA"}

    clock = lambda: datetime(2026, 11, 11, 12, tzinfo=ET)  # noqa: E731
    e = er.EarningsService(MemoryStore(), Quotes(), symbols, "", clock, client, nasdaq=True)
    e.pace = 0
    e.nasdaq = {"NVDA": {"date": "2026-11-24", "time": "amc", "source": "Nasdaq"}}
    assert await e.refresh() == 1
    assert e.info("NVDA")["date"] == "2026-11-24" and "Nasdaq" in e.error
