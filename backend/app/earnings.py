"""Earnings dates for single stocks: when the next report is, and before the open or after
the close.

Sources:
- Dates entered in Settings (one line each: SYMBOL YYYY-MM-DD bmo|amc). These win.
- Finnhub's earnings calendar API, when FINNHUB_API_KEY is set (free key at finnhub.io).
Without a key only the dates entered in Settings are known; a stock with no date is not
opened by the scanner, so the earnings buffer always holds.

ETFs and indexes have no earnings and are not looked up. Refreshed every six hours for the
stocks on the watchlist, held in either account, or allowed live.
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


def parse_finnhub(payload: dict, today: date) -> dict:
    """Finnhub's /calendar/earnings answer: the next report per symbol."""
    out = {}
    for e in (payload or {}).get("earningsCalendar") or []:
        sym, d = (e.get("symbol") or "").upper(), e.get("date")
        if not sym or not d or d < today.isoformat():
            continue
        hour = (e.get("hour") or "").lower()
        info = {"date": d, "time": hour if hour in ("bmo", "amc") else None, "source": "Finnhub"}
        if sym not in out or d < out[sym]["date"]:
            out[sym] = info
    return out


class EarningsService:
    def __init__(
        self,
        store,
        source,
        symbols,
        finnhub_key: str = "",
        clock=lambda: datetime.now(ET),
        client=None,
        demo: bool = False,
    ):
        self.store = store
        self.source = source  # for quotes: which symbols are funds
        self.symbols = symbols  # async () -> set of symbols to cover
        self.finnhub_key = finnhub_key
        self.clock = clock
        self.client = client  # an httpx.AsyncClient, injected by tests
        self.demo = demo
        self.cache: dict = {}
        self.manual: dict = {}
        self.funds: set[str] = set()
        self.industries: dict[str, str] = {}  # stock -> Finnhub industry, for the Portfolio screen
        self.past: dict[str, str] = {}  # stock -> its last report date, once past
        self.refreshed_at: float | None = None
        self.error: str | None = None
        self._wake = asyncio.Event()
        self.pace = 1.0  # spacing between requests to the source; tests set 0

    @property
    def provider(self) -> str:
        return "demo" if self.demo else "Finnhub" if self.finnhub_key else "manual"

    async def load(self) -> None:
        saved = await self.store.get_setting(CACHE_KEY) or {}
        self.cache = saved.get("symbols", {})
        self.funds = set(saved.get("funds", []))
        self.industries = saved.get("industries", {})
        self.past = saved.get("past", {})
        self.refreshed_at = saved.get("refreshed_at")
        self.manual, _ = parse_manual(await self.store.get_setting(MANUAL_KEY) or "", self.today())

    def today(self) -> date:
        return self.clock().astimezone(ET).date()

    def raw(self, symbol: str) -> dict | None:
        today = self.today().isoformat()
        for info in (self.manual.get(symbol), self.cache.get(symbol)):
            if info and info["date"] >= today:
                return info
        return None

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
        for info in (self.manual.get(symbol), self.cache.get(symbol)):
            if info and info["date"] < today:
                dates.append(info["date"])
        dates = [d for d in dates if d]
        return max(dates) if dates else None

    def is_fund(self, symbol: str) -> bool:
        return symbol in self.funds or symbol.startswith("$")

    def known(self, symbol: str) -> bool:
        return self.raw(symbol) is not None

    async def set_manual(self, text: str) -> list[str]:
        manual, bad = parse_manual(text, self.today())
        await self.store.set_setting(MANUAL_KEY, text)
        self.manual = manual
        return bad

    async def manual_text(self) -> str:
        return await self.store.get_setting(MANUAL_KEY) or ""

    def status(self) -> dict:
        return {
            "provider": self.provider,
            "refreshed_at": self.refreshed_at,
            "error": self.error,
            "known": len([s for s in self.cache if self.raw(s)]),
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
        found: dict = {}
        if stocks and self.finnhub_key and not self.demo:
            own = self.client is None
            client = self.client or httpx.AsyncClient(timeout=20)
            try:
                found = await self._finnhub(client, stocks)
                await self._industries(client, stocks - set(self.industries))
            finally:
                if own:
                    await client.aclose()
        elif self.demo:
            found = self._demo(stocks)
        today = self.today().isoformat()
        # Keep a known future date if this pass missed it (a source hiccup), drop past ones.
        for sym, info in list(self.cache.items()):
            if info["date"] < today:
                # Remembered as the last report, so a gap after it is not taken for a shock.
                self.past[sym] = info["date"]
                del self.cache[sym]
        self.cache.update(found)
        self.refreshed_at = time.time()
        self.error = None
        await self.store.set_setting(
            CACHE_KEY,
            {
                "symbols": self.cache,
                "funds": sorted(self.funds),
                "industries": self.industries,
                "past": self.past,
                "refreshed_at": self.refreshed_at,
            },
        )
        return len(found)

    async def _finnhub(self, client, stocks: set[str]) -> dict:
        today = self.today()
        out = {}
        for sym in sorted(stocks):
            r = await client.get(
                "https://finnhub.io/api/v1/calendar/earnings",
                params={
                    "symbol": sym,
                    "from": today.isoformat(),
                    "to": (today + timedelta(days=LOOKAHEAD_DAYS)).isoformat(),
                },
                # In a header, not the address, so the key stays out of logs.
                headers={"X-Finnhub-Token": self.finnhub_key},
            )
            if r.status_code == 429:
                await asyncio.sleep(2 * self.pace)
                continue
            r.raise_for_status()
            out.update(parse_finnhub(r.json(), today))
            await asyncio.sleep(1.1 * self.pace)  # the free plan allows 60 a minute
        return out

    async def _industries(self, client, stocks: set[str]) -> None:
        """Each stock's industry from Finnhub's company profile, looked up once and kept."""
        for sym in sorted(stocks):
            r = await client.get(
                "https://finnhub.io/api/v1/stock/profile2",
                params={"symbol": sym},
                headers={"X-Finnhub-Token": self.finnhub_key},
            )
            if r.status_code == 429:
                await asyncio.sleep(2 * self.pace)
                continue
            if r.status_code != 200:
                continue
            industry = (r.json() or {}).get("finnhubIndustry")
            if industry:
                self.industries[sym] = industry
            await asyncio.sleep(1.1 * self.pace)

    def _demo(self, stocks: set[str]) -> dict:
        """Demo mode: a report about three weeks out, after the close."""
        d = self.today() + timedelta(days=21)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return {s: {"date": d.isoformat(), "time": "amc", "source": "demo"} for s in stocks}
