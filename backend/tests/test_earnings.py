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


def test_parsers_read_your_dates_and_nasdaq_industries():
    manual, bad = er.parse_manual("aapl 2027-01-28 AMC\n# note\nTSLA 2027-01-21\nnope", TODAY)
    assert manual["AAPL"]["time"] == "amc" and manual["TSLA"]["time"] is None
    assert bad == ["nope"]
    screener = {
        "data": {
            "rows": [
                {"symbol": "NVDA", "industry": "Semiconductors"},
                {"symbol": "BRK/B", "industry": "Major Insurers"},
                {"symbol": "XYZ", "industry": ""},
            ]
        }
    }
    out = er.parse_screener(screener)
    assert out == {"NVDA": "Semiconductors", "BRK/B": "Major Insurers", "BRK.B": "Major Insurers"}
    assert er.parse_screener({"data": None}) == {}


class Quotes:
    async def quotes(self, symbols):
        return {
            s: {"assetMainType": "EQUITY", "assetSubType": "ETF" if s == "SPY" else None}
            for s in symbols
        }


@pytest.mark.anyio
async def test_industries_from_nasdaq_once_a_day_and_your_dates_win():
    asked = []

    def handler(request: httpx.Request):
        assert request.url.host == "api.nasdaq.com"
        asked.append(request.url.path)
        if request.url.path.endswith("/screener/stocks"):
            rows = [{"symbol": "NVDA", "industry": "Semiconductors"}]
            return httpx.Response(200, json={"data": {"rows": rows}})
        d = request.url.params["date"]
        rows = [{"symbol": "NVDA", "time": "time-after-hours"}] if d == "2026-11-19" else []
        return httpx.Response(200, json={"data": {"rows": rows}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def symbols():
        return {"SPY", "NVDA", "$SPX"}

    clock = lambda: datetime(2026, 11, 11, 12, tzinfo=ET)  # noqa: E731
    store = MemoryStore()
    e = er.EarningsService(store, Quotes(), symbols, clock=clock, client=client, nasdaq=True)
    e.pace = 0
    assert await e.refresh() == 1
    assert e.info("NVDA")["source"] == "Nasdaq" and e.info("SPY") is None
    assert e.is_fund("SPY") and e.is_fund("$SPX")
    assert e.industries == {"NVDA": "Semiconductors"}  # for the Portfolio screen's sectors
    assert asked.count("/api/screener/stocks") == 1
    await e.refresh()
    assert asked.count("/api/screener/stocks") == 1  # once a day
    await e.set_manual("NVDA 2026-11-20 bmo")
    assert e.info("NVDA")["source"] == "you"  # yours win over the source
    # Without Nasdaq (tests, or no network): only your dates.
    manual = er.EarningsService(MemoryStore(), Quotes(), symbols, clock=clock)
    assert await manual.refresh() == 0 and manual.info("NVDA") is None
    assert manual.provider == "manual"


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
    e = er.EarningsService(store, Quotes(), symbols, clock=clock, client=client, nasdaq=True)
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
    again = er.EarningsService(store, Quotes(), symbols, clock=clock, client=client, nasdaq=True)
    await again.load()
    assert again.info("NVDA")["date"] == "2026-11-24" and again.last_report("AAPL") == "2026-10-29"


@pytest.mark.anyio
async def test_your_dates_beat_nasdaq_and_can_be_removed():
    clock = lambda: datetime(2026, 11, 11, 12, tzinfo=ET)  # noqa: E731

    async def symbols():
        return set()

    e = er.EarningsService(MemoryStore(), Quotes(), symbols, clock=clock)
    e.nasdaq = {"X": {"date": "2026-12-03", "time": "amc", "source": "Nasdaq"}}
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
    store = MemoryStore()
    e = er.EarningsService(store, Quotes(), symbols, clock=clock, client=client, nasdaq=True)
    e.pace = 0
    e.nasdaq = {"NVDA": {"date": "2026-11-24", "time": "amc", "source": "Nasdaq"}}
    assert await e.refresh() == 1
    assert e.info("NVDA")["date"] == "2026-11-24" and "Nasdaq" in e.error


@pytest.mark.anyio
async def test_nasdaq_dropped_connections_are_retried_and_one_bad_day_is_skipped():
    tries = {}

    def handler(request):
        d = request.url.params["date"]
        tries[d] = tries.get(d, 0) + 1
        if d == "2026-11-12" and tries[d] == 1:
            raise httpx.ReadError("", request=request)  # dropped once: retried
        if d == "2026-11-13":
            raise httpx.ReadError("", request=request)  # always: skipped
        if d > "2026-11-20":
            return httpx.Response(200, json={"data": None})
        rows = [{"symbol": "NVDA", "time": "time-after-hours"}] if d == "2026-11-19" else []
        if d == "2026-11-12":
            rows = [{"symbol": "WMT", "time": "time-pre-market"}]
        return httpx.Response(200, json={"data": {"rows": rows}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def symbols():
        return {"NVDA", "WMT", "AAPL"}

    clock = lambda: datetime(2026, 11, 11, 12, tzinfo=ET)  # noqa: E731
    e = er.EarningsService(
        MemoryStore(), Quotes(), symbols, clock=clock, client=client, nasdaq=True
    )
    e.pace = 0
    # A date known from the last pass, on the day that cannot be read now, is kept.
    e.nasdaq = {"AAPL": {"date": "2026-11-13", "time": "bmo", "source": "Nasdaq"}}
    assert await e.refresh() == 3
    assert tries["2026-11-12"] == 2 and tries["2026-11-13"] == er.NASDAQ_TRIES
    assert e.info("WMT")["date"] == "2026-11-12" and e.info("AAPL")["date"] == "2026-11-13"
    assert e.error.startswith("Nasdaq: ") and "(ReadError)" in e.error
    assert "2026-11-13" not in e.nasdaq_done  # read again next time (it is ahead anyway)


def test_errors_with_no_text_still_say_what_they_are():
    assert er._why(httpx.ReadError("")) == "ReadError"
    assert er._why(ValueError("bad")) == "ValueError: bad"


@pytest.mark.anyio
async def test_a_date_once_read_is_kept_until_it_passes():
    """Nasdaq drops a company from a day's calendar now and then (COF, GM, GE, UAL and NFLX
    lost their dates that way): the date read before stays until Nasdaq gives another."""
    seen: list = []
    days = {d.isoformat(): [] for d in er.weekdays(date(2026, 10, 1), date(2026, 12, 31))}
    days["2026-11-20"] = [("COF", "time-after-hours"), ("GM", "time-pre-market")]
    days["2026-11-13"] = [("UAL", "time-after-hours")]
    client = httpx.AsyncClient(transport=httpx.MockTransport(_nasdaq_handler(days, seen)))
    now = [datetime(2026, 11, 11, 12, tzinfo=ET)]

    async def symbols():
        return {"COF", "GM", "UAL"}

    store = MemoryStore()
    e = er.EarningsService(
        store, Quotes(), symbols, clock=lambda: now[0], client=client, nasdaq=True
    )
    e.pace = 0
    await e.refresh()
    assert e.info("COF")["date"] == "2026-11-20" and e.info("UAL")["date"] == "2026-11-13"
    # Next pass: COF missing from its day, GM moved a week later.
    days["2026-11-20"] = []
    days["2026-11-27"] = [("GM", "time-pre-market")]
    await e.refresh()
    assert e.info("COF")["date"] == "2026-11-20" and e.info("COF")["time"] == "amc"
    assert e.info("GM")["date"] == "2026-11-27"
    # Kept across a restart too.
    again = er.EarningsService(
        store, Quotes(), symbols, clock=lambda: now[0], client=client, nasdaq=True
    )
    await again.load()
    assert again.info("COF")["date"] == "2026-11-20"
    # Once passed, a kept date is the last report, not the next.
    now[0] = datetime(2026, 11, 16, 12, tzinfo=ET)
    await e.refresh()
    assert e.info("UAL") is None and e.last_report("UAL") == "2026-11-13"
