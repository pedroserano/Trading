"""Earnings dates for single stocks: when the next report is, and before the open or after
the close.

Sources:
- Dates you enter on the Watchlist (stored as lines: SYMBOL YYYY-MM-DD bmo|amc). These win.
- Nasdaq's earnings calendar (free, no key): one request per weekday, about five weeks
  ahead (dates companies have announced, with before the open or after the close), and the
  last few weeks back, so a stock that has just reported is known to have.
Each stock's industry (for the Portfolio screen's sectors and the scanner's per-sector cap)
comes from Nasdaq's stock screener, one request for every listed company, once a day.

A stock with no coming date known but a report in the last few weeks has, by the quarterly
cycle, none for a while: the scanner may treat it as clear until a set number of days after
that report (quiet_until). Otherwise a stock with no date is not opened by the scanner.

ETFs and indexes have no earnings and are not looked up. Refreshed every six hours.
"""

import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
CACHE_KEY = "earnings"
MANUAL_KEY = "earnings_manual"
LOOKAHEAD_DAYS = 100
NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings"
SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
INDUSTRIES_REFRESH_S = 24 * 3600
NASDAQ_AHEAD_DAYS = 45  # Nasdaq lists about five weeks ahead; empty beyond
NASDAQ_BACK_DAYS = 40  # how far back the last reports are read (past days once each)
PAST_KEEP_DAYS = 200
NASDAQ_TIMES = {"time-pre-market": "bmo", "time-after-hours": "amc"}
# Nasdaq answers a browser, not a bare client. Nothing about you is sent.
NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
NASDAQ_TRIES = 3  # per day, on a dropped connection, a timeout or a 5xx


def _why(e: Exception) -> str:
    """An error as text, also when it carries none (httpx's ReadError often does not)."""
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


REFRESH_S = 6 * 3600
FUNDS = {"ETF", "ETN", "COLLECTIVE_INVESTMENT", "INDEX", "MUTUAL_FUND"}
LABELS = {"bmo": "before the open", "amc": "after the close", None: "time not given"}


def previous_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def last_session(info: dict) -> date:
    """The last session that holds a position before the report reaches the price: the
    report day itself when it comes after the close, the session before otherwise (before
    the open, or no time given, which is treated as before the open)."""
    d = date.fromisoformat(info["date"])
    return d if info.get("time") == "amc" else previous_weekday(d)


def sessions_until(today: date, d: date) -> int:
    """Weekdays from today to d: 0 when d is today. Negative when d has passed."""
    if d < today:
        return -1
    n, cur = 0, today
    while cur < d:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def describe(info: dict | None, today: date) -> dict | None:
    """What screens and rules need: the date, before/after, and sessions left to exit."""
    if not info:
        return None
    exit_by = last_session(info)
    return {
        "date": info["date"],
        "time": info.get("time"),
        "label": LABELS.get(info.get("time"), LABELS[None]),
        "source": info.get("source"),
        "days": (date.fromisoformat(info["date"]) - today).days,
        "exit_by": exit_by.isoformat(),
        "sessions_left": sessions_until(today, exit_by),
    }


def parse_manual(text: str, today: date) -> tuple[dict, list[str]]:
    """{SYMBOL: info} from the Settings text, and the lines that could not be read."""
    out, bad = {}, []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(
            r"([A-Za-z.\/]+)[\s,]+(\d{4}-\d{2}-\d{2})(?:[\s,]+(bmo|amc))?", line, re.IGNORECASE
        )
        if not m:
            bad.append(line)
            continue
        try:
            d = date.fromisoformat(m.group(2))
        except ValueError:
            bad.append(line)
            continue
        out[m.group(1).upper()] = {
            "date": d.isoformat(),
            "time": (m.group(3) or "").lower() or None,
            "source": "you",
        }
    return out, bad


def parse_nasdaq(payload: dict) -> dict | None:
    """One day of Nasdaq's calendar: {SYMBOL: "bmo"|"amc"|None}. None when Nasdaq has no
    calendar for the day (too far ahead)."""
    data = (payload or {}).get("data")
    if data is None:
        return None
    out = {}
    for r in data.get("rows") or []:
        sym = (r.get("symbol") or "").strip().upper()
        if sym:
            out[sym] = NASDAQ_TIMES.get(r.get("time"))
    return out


def parse_screener(payload: dict) -> dict:
    """Nasdaq's screener: {SYMBOL: industry}. Its class shares are written BRK/B; Schwab's
    BRK/B too, and BRK.B is kept as well."""
    out = {}
    for r in ((payload or {}).get("data") or {}).get("rows") or []:
        sym = (r.get("symbol") or "").strip().upper()
        industry = (r.get("industry") or "").strip()
        if sym and industry:
            out[sym] = industry
            if "/" in sym:
                out[sym.replace("/", ".")] = industry
    return out


def weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def manual_lines(manual: dict) -> str:
    return "\n".join(
        f"{sym} {info['date']}" + (f" {info['time']}" if info.get("time") else "")
        for sym, info in sorted(manual.items())
    )


class EarningsService:
    def __init__(
        self,
        store,
        source,
        symbols,
        clock=lambda: datetime.now(ET),
        client=None,
        demo: bool = False,
        nasdaq: bool = False,
    ):
        self.store = store
        self.source = source  # for quotes: which symbols are funds
        self.symbols = symbols  # async () -> set of symbols to cover
        self.clock = clock
        self.client = client  # an httpx.AsyncClient, injected by tests
        self.demo = demo
        self.use_nasdaq = nasdaq and not demo
        self.nasdaq: dict = {}  # Nasdaq: stock -> next report in its window
        self.nasdaq_done: set[str] = set()  # past days already read (ISO)
        self.manual: dict = {}
        self.funds: set[str] = set()
        self.industries: dict[str, str] = {}  # stock -> Nasdaq industry, for the Portfolio screen
        self.industries_at: float = 0.0
        self.past: dict[str, str] = {}  # stock -> its last report date, once past
        self.stocks: set[str] = set()  # the stocks covered on the last refresh
        self.partial: str | None = None  # days of the calendar a refresh could not read
        self.refreshed_at: float | None = None
        self.error: str | None = None
        self._wake = asyncio.Event()
        self.pace = 1.0  # spacing between requests to the source; tests set 0

    @property
    def provider(self) -> str:
        if self.demo:
            return "demo"
        return "Nasdaq" if self.use_nasdaq else "manual"

    async def load(self) -> None:
        saved = await self.store.get_setting(CACHE_KEY) or {}
        self.funds = set(saved.get("funds", []))
        self.industries = saved.get("industries", {})
        self.industries_at = saved.get("industries_at", 0.0)
        self.past = saved.get("past", {})
        self.nasdaq = saved.get("nasdaq", {})
        self.nasdaq_done = set(saved.get("nasdaq_done", []))
        self.refreshed_at = saved.get("refreshed_at")
        self.manual, _ = parse_manual(await self.store.get_setting(MANUAL_KEY) or "", self.today())

    def today(self) -> date:
        return self.clock().astimezone(ET).date()

    def raw(self, symbol: str) -> dict | None:
        today = self.today().isoformat()
        mine = self.manual.get(symbol)
        if mine and mine["date"] >= today:
            return mine
        info = self.nasdaq.get(symbol)
        return info if info and info["date"] >= today else None

    def info(self, symbol: str) -> dict | None:
        """The next report for a stock, described for screens and rules; None for funds,
        and for stocks with no date known."""
        if symbol in self.funds or symbol.startswith("$"):
            return None
        return describe(self.raw(symbol), self.today())

    def last_report(self, symbol: str) -> str | None:
        """The most recent report date already past (ISO), when known."""
        today = self.today().isoformat()
        dates = [self.past.get(symbol)]
        for info in (self.manual.get(symbol), self.nasdaq.get(symbol)):
            if info and info["date"] < today:
                dates.append(info["date"])
        dates = [d for d in dates if d]
        return max(dates) if dates else None

    def quiet_until(self, symbol: str, recent_days: int, gap_days: int) -> dict | None:
        """For a stock with no coming report known that reported in the last `recent_days`:
        {"last": that report, "until": the report plus `gap_days`}, the quarterly cycle's
        least gap to the next. None otherwise, or when either is 0 (off)."""
        if not recent_days or not gap_days or self.is_fund(symbol) or self.raw(symbol):
            return None
        last = self.last_report(symbol)
        if not last:
            return None
        d = date.fromisoformat(last)
        if (self.today() - d).days > recent_days:
            return None
        return {"last": last, "until": (d + timedelta(days=gap_days)).isoformat()}

    def is_fund(self, symbol: str) -> bool:
        return symbol in self.funds or symbol.startswith("$")

    def known(self, symbol: str) -> bool:
        return self.raw(symbol) is not None

    async def set_manual(self, text: str) -> list[str]:
        manual, bad = parse_manual(text, self.today())
        await self.store.set_setting(MANUAL_KEY, text)
        self.manual = manual
        return bad

    async def set_one(self, symbol: str, day: str | None, time_: str | None) -> None:
        """Sets (or with no day, removes) your date for one stock."""
        manual, _ = parse_manual(await self.manual_text(), self.today())
        if day:
            date.fromisoformat(day)  # ValueError for a bad date
            manual[symbol] = {
                "date": day,
                "time": time_ if time_ in ("bmo", "amc") else None,
                "source": "you",
            }
        else:
            manual.pop(symbol, None)
        await self.set_manual(manual_lines(manual))

    async def manual_text(self) -> str:
        return await self.store.get_setting(MANUAL_KEY) or ""

    def status(self) -> dict:
        return {
            "provider": self.provider,
            "refreshed_at": self.refreshed_at,
            "error": self.error,
            "known": len([s for s in self.stocks if self.raw(s)]),
            "stocks": len(self.stocks),
        }

    def refresh_soon(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        await self.load()
        while True:
            self._wake.clear()
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Earnings refresh failed")
                self.error = f"{type(e).__name__}: {e}"
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=REFRESH_S)
            except TimeoutError:
                pass

    async def refresh(self) -> int:
        """Looks up the next report of every stock covered. Returns how many were found."""
        wanted = {s for s in await self.symbols() if s and not s.startswith("$")}
        try:
            quotes = await self.source.quotes(sorted(wanted)) if wanted else {}
            for sym, q in quotes.items():
                kind = {q.get("assetMainType"), q.get("assetSubType"), q.get("assetType")}
                if kind & FUNDS:
                    self.funds.add(sym)
        except Exception as e:
            log.warning("Earnings: could not tell funds from stocks: %s", e)
        stocks = wanted - self.funds
        self.stocks = stocks
        errors = []
        self.partial = None
        if stocks and self.use_nasdaq:
            own = self.client is None
            client = self.client or httpx.AsyncClient(timeout=30)
            try:
                try:
                    await self._nasdaq(client)
                except Exception as e:  # keeps what it had
                    log.warning("Earnings: Nasdaq calendar failed: %s", e)
                    errors.append(f"Nasdaq: {_why(e)}"[:200])
                if stocks - set(self.industries) and (
                    time.time() - self.industries_at > INDUSTRIES_REFRESH_S
                ):
                    try:
                        await self._industries(client)
                    except Exception as e:  # sectors are a refinement
                        log.warning("Earnings: Nasdaq industries failed: %s", e)
            finally:
                if own:
                    await client.aclose()
        elif self.demo:
            self.nasdaq = self._demo(stocks)
        self.refreshed_at = time.time()
        if self.partial:
            errors.append(self.partial)
        self.error = "; ".join(errors) or None
        await self.store.set_setting(
            CACHE_KEY,
            {
                "nasdaq": self.nasdaq,
                "nasdaq_done": sorted(self.nasdaq_done),
                "funds": sorted(self.funds),
                "industries": self.industries,
                "industries_at": self.industries_at,
                "past": self.past,
                "refreshed_at": self.refreshed_at,
            },
        )
        return len([s for s in stocks if self.raw(s)])

    async def _nasdaq_day(self, client, d: date) -> dict | None:
        """One day of the calendar, tried again on a dropped connection, a timeout or a
        server error. Raises once every try failed."""
        for attempt in range(NASDAQ_TRIES):
            try:
                r = await client.get(
                    NASDAQ_URL,
                    params={"date": d.isoformat()},
                    headers=NASDAQ_HEADERS,
                    timeout=15,
                )
                if r.status_code >= 500 or r.status_code == 429:
                    raise httpx.HTTPStatusError(
                        f"HTTP {r.status_code}", request=r.request, response=r
                    )
                r.raise_for_status()
                return parse_nasdaq(r.json())
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if attempt == NASDAQ_TRIES - 1 or (
                    isinstance(e, httpx.HTTPStatusError)
                    and e.response.status_code < 500
                    and e.response.status_code != 429
                ):
                    raise
                await asyncio.sleep(2 * (attempt + 1) * self.pace)
        return None  # not reached

    async def _nasdaq(self, client) -> None:
        """Nasdaq's calendar, every listed company: the weeks ahead (read again each time,
        dates move) and the weeks back, for each stock's last report (each day read once).
        A day that still fails is skipped (its dates kept from before, or read next time);
        if nothing at all can be read, the error is raised."""
        today = self.today()
        ahead: dict = {}
        empty = 0
        failed: list[str] = []
        read = 0
        last_error = None
        for d in weekdays(today, today + timedelta(days=NASDAQ_AHEAD_DAYS)):
            try:
                day = await self._nasdaq_day(client, d)
            except Exception as e:
                failed.append(d.isoformat())
                last_error = e
                if not read and len(failed) >= 3:
                    raise  # not reaching Nasdaq at all
                continue
            finally:
                await asyncio.sleep(0.5 * self.pace)
            read += 1
            if day is None:
                empty += 1
                if empty >= 3:  # past the end of what Nasdaq lists
                    break
                continue
            empty = 0
            for sym, t in day.items():
                if sym not in ahead:
                    ahead[sym] = {"date": d.isoformat(), "time": t, "source": "Nasdaq"}
        # Dates on the days that could not be read are kept from the last pass.
        for sym, info in self.nasdaq.items():
            if info["date"] in failed and (sym not in ahead or info["date"] < ahead[sym]["date"]):
                ahead[sym] = info
        self.nasdaq = ahead
        for d in weekdays(today - timedelta(days=NASDAQ_BACK_DAYS), today - timedelta(days=1)):
            if d.isoformat() in self.nasdaq_done:
                continue
            try:
                day = await self._nasdaq_day(client, d)
            except Exception as e:
                failed.append(d.isoformat())
                last_error = e
                continue  # read again next time
            finally:
                await asyncio.sleep(0.5 * self.pace)
            if day is None:
                continue
            for sym in day:
                if self.past.get(sym, "") < d.isoformat():
                    self.past[sym] = d.isoformat()
            self.nasdaq_done.add(d.isoformat())
        self.partial = (
            f"Nasdaq: {len(failed)} day{'s' if len(failed) != 1 else ''} not read "
            f"({_why(last_error)}); tried again next refresh"
            if failed
            else None
        )
        oldest = (today - timedelta(days=PAST_KEEP_DAYS)).isoformat()
        self.nasdaq_done = {x for x in self.nasdaq_done if x >= oldest}
        self.past = {k: v for k, v in self.past.items() if v >= oldest}

    async def _industries(self, client) -> None:
        """Every listed stock's industry, from Nasdaq's screener in one request."""
        r = await client.get(
            SCREENER_URL,
            params={"tableonly": "true", "download": "true"},
            headers=NASDAQ_HEADERS,
        )
        r.raise_for_status()
        self.industries.update(parse_screener(r.json()))
        self.industries_at = time.time()

    def _demo(self, stocks: set[str]) -> dict:
        """Demo mode: a report about three weeks out, after the close."""
        d = self.today() + timedelta(days=21)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return {s: {"date": d.isoformat(), "time": "amc", "source": "demo"} for s in stocks}
