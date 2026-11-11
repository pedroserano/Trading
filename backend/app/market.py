"""The market strip: price and day's move for the major futures, as tastylive runs through them.

Futures are asked for by root (/ES), never by month: Schwab answers a root with its active
contract and moves it forward at the roll, so no month code can go stale here. If Schwab ever
stops resolving roots, the front contract is worked out from each market's standard cycle;
if that fails too, a cash index or ETF stands in and is marked as such.
"""

import asyncio
import calendar
import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
POLL_S = 15
RECHECK_ROOTS_S = 3600

# (label, symbol, stand-in when the future cannot be quoted)
MARKETS = [
    ("S&P", "/ES", "$SPX"),
    ("Nasdaq", "/NQ", "$COMPX"),
    ("Russell", "/RTY", "$RUT"),
    ("Dow", "/YM", "$DJI"),
    ("VIX", "$VIX", None),
    ("Crude", "/CL", "USO"),
    ("Gold", "/GC", "GLD"),
    ("Silver", "/SI", "SLV"),
    ("30-yr", "/ZB", "TLT"),
    ("10-yr", "/ZN", "IEF"),
    ("Euro", "/6E", "FXE"),
    ("Yen", "/6J", "FXY"),
    ("Bitcoin", "/BTC", "IBIT"),
]
CODES = "FGHJKMNQUVXZ"  # month codes, January to December
QUARTERLY = {"ES", "NQ", "RTY", "YM", "ZB", "ZN", "6E", "6J"}
ACTIVE_MONTHS = {"GC": (2, 4, 6, 8, 10, 12), "SI": (3, 5, 7, 9, 12)}


def _third_friday(y: int, m: int) -> date:
    d = date(y, m, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def _last_friday(y: int, m: int) -> date:
    d = date(y, m, calendar.monthrange(y, m)[1])
    return d - timedelta(days=(d.weekday() - 4) % 7)


def _business_days_before(d: date, n: int) -> date:
    while n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _months(today: date, months: tuple[int, ...] | None = None):
    """(year, month) from this month on, optionally only the given months."""
    y, m = today.year, today.month
    for _ in range(30):
        if months is None or m in months:
            yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def _prev_month(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def front_contract(root: str, today: date) -> str:
    """The front contract by each market's standard cycle and roll timing, e.g. /ESZ26.

    An approximation of where volume moves, used only when Schwab will not resolve a root.
    """
    if root in QUARTERLY:
        months, roll = (3, 6, 9, 12), lambda y, m: _third_friday(y, m) - timedelta(days=8)
    elif root == "CL":
        # Delivery month m stops trading 3 business days before the 25th of the month before.
        months = None

        def roll(y, m):
            py, pm = _prev_month(y, m)
            return _business_days_before(date(py, pm, 25), 5)
    elif root in ACTIVE_MONTHS:
        months = ACTIVE_MONTHS[root]

        def roll(y, m):
            py, pm = _prev_month(y, m)
            return date(py, pm, 25)
    elif root == "BTC":
        months, roll = None, lambda y, m: _last_friday(y, m) - timedelta(days=5)
    else:
        raise ValueError(f"No contract cycle for {root}")
    for y, m in _months(today, months):
        if today < roll(y, m):
            return f"/{root}{CODES[m - 1]}{y % 100:02d}"
    raise ValueError(f"No front contract found for {root}")


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN


def priced(entry: dict | None) -> bool:
    q = (entry or {}).get("quote") or {}
    return bool(_num(q.get("lastPrice")) or _num(q.get("mark")))


def item(
    label: str, symbol: str, entry: dict | None, contract: str | None = None, fallback: bool = False
) -> dict:
    """One market for the strip, from Schwab's quote entry for it."""
    q = (entry or {}).get("quote") or {}
    ref = (entry or {}).get("reference") or {}
    price = _num(q.get("lastPrice")) or _num(q.get("mark"))
    change = _num(q.get("netChange"))
    pct = None
    if price is not None and change is not None and price - change:
        # From the points change: the same meaning for futures, indexes and ETFs.
        pct = change / (price - change) * 100
    elif _num(q.get("netPercentChange")) is not None:
        pct = _num(q.get("netPercentChange"))
    return {
        "label": label,
        "symbol": symbol,
        "contract": contract
        or ref.get("futureActiveSymbol")
        or q.get("futureActiveSymbol")
        or (entry or {}).get("symbol")
        or symbol,
        "price": price,
        "change": None if change is None else round(change, 6),
        "change_pct": None if pct is None else round(pct, 2),
        "fallback": fallback,
    }


class MarketService:
    def __init__(self, source, clock=lambda: datetime.now(ET)):
        self.source = source
        self.clock = clock
        self.items: list[dict] = []
        self.at: float | None = None
        self.error: str | None = None
        # Per root: "root" (Schwab resolves it), "contract" (worked out here) or "fallback".
        self.route: dict[str, str] = {}
        self.route_checked = 0.0

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                log.info("Market strip not refreshed: %s", e)
            await asyncio.sleep(POLL_S)

    async def refresh(self) -> list[dict]:
        today = self.clock().astimezone(ET).date()
        if time.time() - self.route_checked > RECHECK_ROOTS_S:
            self.route, self.route_checked = {}, time.time()
        roots = [s for _, s, _ in MARKETS if self.route.get(s, "root") == "root"]
        quotes = await self.source.quotes(roots) if roots else {}
        for s in roots:
            if s.startswith("/") and not priced(quotes.get(s)):
                self.route[s] = "contract"
        contracts: dict[str, str] = {}
        for _, s, _ in MARKETS:
            if self.route.get(s) == "contract":
                try:
                    contracts[s] = front_contract(s[1:], today)
                except ValueError:
                    self.route[s] = "fallback"
        if contracts:
            got = await self.source.quotes(sorted(set(contracts.values())))
            for s, c in contracts.items():
                if priced(got.get(c)):
                    quotes[c] = got[c]
                else:
                    self.route[s] = "fallback"
        stand_ins = [f for _, s, f in MARKETS if f and self.route.get(s) == "fallback"]
        if stand_ins:
            quotes.update(await self.source.quotes(stand_ins))
        out = []
        for label, s, f in MARKETS:
            how = self.route.get(s, "root")
            if how == "contract":
                out.append(item(label, s, quotes.get(contracts[s]), contract=contracts[s]))
            elif how == "fallback" and f:
                out.append(item(label, f, quotes.get(f), fallback=True))
            else:
                out.append(item(label, s, quotes.get(s)))
        self.items, self.at, self.error = out, time.time(), None
        return out

    def view(self) -> dict:
        return {"items": self.items, "at": self.at, "error": self.error}
