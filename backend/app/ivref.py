"""A year of IV range from DoltHub, for symbols the app has not recorded for a year yet.

The app records each underlying's 30-day IV once a day from Schwab's chains, but Schwab has no
past IV, so an IV rank of its own takes a year to mean much. Until a symbol's recorded history
spans a year, its rank comes from DoltHub's free `post-no-preference/options` database: each
day's IV with its 52-week high and low, per symbol. DoltHub's rank is used as it stands (its
IV against its own range), as of its last close, because its IV is worked out differently from
the app's and the two are not mixed. Once the app's own history spans a year, it takes over.
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
URL = "https://www.dolthub.com/api/v1alpha1/post-no-preference/options/master"
CACHE_KEY = "ivref_dolthub"
REFRESH_S = 6 * 3600
BATCH = 20  # symbols per query: a date and a short list answers in a second or two
LOOKBACK_DAYS = 10  # the latest day DoltHub has, looked for this far back
FILL_DAYS = 3  # trading days read, for symbols whose latest row lacks the year range
MAX_AGE_DAYS = 7  # older than this and the rank is not used: the source has stopped
OWN_YEAR_DAYS = 365  # the app's own history takes over once it spans this
# Only plain tickers go into a query, so a symbol can never change the SQL.
_TICKER = re.compile(r"^[A-Z0-9.]{1,12}$")


def dolt_symbol(sym: str) -> str:
    return sym.replace("/", ".")


def rank_from(entry: dict) -> dict | None:
    """An IV rank in the app's shape (percent units) from one DoltHub row."""
    iv, hi, lo = entry.get("iv"), entry.get("high"), entry.get("low")
    if iv is None or hi is None or lo is None:
        return None
    iv, hi, lo = iv * 100, hi * 100, lo * 100
    rank = 0.0 if hi == lo else (iv - lo) / (hi - lo) * 100
    return {
        "rank": round(max(0.0, min(100.0, rank)), 1),
        "percentile": None,
        "days": None,
        "low": round(lo, 2),
        "high": round(hi, 2),
        "iv": round(iv, 2),
        "source": "DoltHub",
        "as_of": entry["date"],
    }


def choose(own: dict | None, own_since: date | None, ref: dict | None, today: date) -> dict | None:
    """The app's own rank once its history spans a year, else DoltHub's while it is fresh,
    else the app's own (shown only after 20 days, as before)."""
    if own and own_since is not None and (today - own_since).days >= OWN_YEAR_DAYS:
        return own
    if ref and (today - date.fromisoformat(ref["as_of"])).days <= MAX_AGE_DAYS:
        if own:
            ref = {**ref, "recorded_days": own.get("days")}
        return ref
    return own


class IvReference:
    def __init__(self, store, symbols, clock=None, client=None):
        self.store = store
        self.symbols = symbols  # async () -> iterable of symbols
        self.clock = clock or (lambda: datetime.now(ET))
        self.client = client  # an httpx.AsyncClient, injected by tests
        self.data: dict[str, dict] = {}
        self.refreshed_at: float | None = None
        self.error: str | None = None

    async def load(self) -> None:
        saved = await self.store.get_setting(CACHE_KEY) or {}
        self.data = saved.get("symbols", {})
        self.refreshed_at = saved.get("refreshed_at")

    def today(self) -> date:
        return self.clock().astimezone(ET).date()

    def rank(self, symbol: str) -> dict | None:
        e = self.data.get(symbol)
        return rank_from(e) if e else None

    def status(self) -> dict:
        dates = sorted({e["date"] for e in self.data.values()})
        return {
            "symbols": len(self.data),
            "as_of": dates[-1] if dates else None,
            "refreshed_at": self.refreshed_at,
            "error": self.error,
        }

    async def run(self) -> None:
        await self.load()
        while True:
            if not self.refreshed_at or time.time() - self.refreshed_at >= REFRESH_S:
                try:
                    await self.refresh()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("DoltHub IV refresh failed: %s", e)
                    self.error = f"{type(e).__name__}: {e}"
            await asyncio.sleep(600)

    async def _query(self, client, sql: str) -> list[dict]:
        r = await client.get(URL, params={"q": sql})
        r.raise_for_status()
        body = r.json()
        if body.get("query_execution_status") != "Success":
            raise RuntimeError(body.get("query_execution_message") or "query failed")
        return body.get("rows") or []

    async def _trading_days(self, client) -> list[date]:
        """The latest days DoltHub has, newest first, up to FILL_DAYS of them. Weekends are
        skipped without asking: a query for a day it does not have is its slowest."""
        days: list[date] = []
        d = self.today()
        for _ in range(LOOKBACK_DAYS):
            if d.weekday() < 5:
                rows = await self._query(
                    client,
                    "SELECT date FROM volatility_history "  # noqa: S608 - a date and a constant
                    f"WHERE date = '{d.isoformat()}' AND act_symbol = 'SPY'",
                )
                if rows:
                    days.append(d)
                    if len(days) == FILL_DAYS:
                        break
            d -= timedelta(days=1)
        return days

    async def refresh(self) -> int:
        """Reads the latest day's IV and year range for every symbol covered. Returns how many
        were found."""
        wanted = sorted({s for s in await self.symbols() if s and not s.startswith(("$", "/"))})
        if not wanted:
            return 0
        # A symbol with a year of our own IV no longer needs DoltHub: it is not asked for.
        since = self.today() - timedelta(days=OWN_YEAR_DAYS + 1)
        needed = []
        for sym in wanted:
            own_hist = await self.store.iv_history(sym, since)
            if own_hist and (self.today() - own_hist[0][0]).days >= OWN_YEAR_DAYS:
                self.data.pop(sym, None)
                continue
            needed.append(sym)
        names = {dolt_symbol(s): s for s in needed if _TICKER.match(dolt_symbol(s))}
        if not names:
            return 0
        own = self.client is None
        client = self.client or httpx.AsyncClient(timeout=60)
        found: dict[str, dict] = {}
        try:
            days = await self._trading_days(client)
            if not days:
                raise RuntimeError(f"no data in the last {LOOKBACK_DAYS} days")
            latest = days[0]
            missing = list(names)
            # A day's row sometimes lacks the year range: those symbols are read from the
            # trading day before, and the one before that.
            for when in days:
                if not missing:
                    break
                day = when.isoformat()
                for i in range(0, len(missing), BATCH):
                    batch = missing[i : i + BATCH]
                    listed = ",".join(f"'{s}'" for s in batch)
                    rows = await self._query(
                        client,
                        "SELECT act_symbol, date, iv_current, iv_year_high, iv_year_low "  # noqa: S608 - a date and checked tickers
                        f"FROM volatility_history WHERE date = '{day}' "
                        f"AND act_symbol IN ({listed})",
                    )
                    for row in rows:
                        e = {
                            "date": row["date"],
                            "iv": _num(row.get("iv_current")),
                            "high": _num(row.get("iv_year_high")),
                            "low": _num(row.get("iv_year_low")),
                        }
                        if rank_from(e):
                            found[names[row["act_symbol"]]] = e
                missing = [d for d in missing if names[d] not in found]
        finally:
            if own:
                await client.aclose()
        # A symbol missing today keeps its last row; choose() stops using it once it is old.
        self.data.update(found)
        self.refreshed_at = time.time()
        self.error = None
        await self.store.set_setting(
            CACHE_KEY, {"symbols": self.data, "refreshed_at": self.refreshed_at}
        )
        log.info("DoltHub IV: %d of %d symbols as of %s", len(found), len(wanted), latest)
        return len(found)


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
