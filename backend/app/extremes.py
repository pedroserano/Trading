"""Price extremes: where a price sits after an unusual move, for the Watchlist and scanner.

Three measures, each optional when its data is missing:
- 52-week range: how close the price is to its one-year high or low (closing prices).
- Move vs expected: the last few sessions' move in standard deviations of the move implied
  volatility expected over that time.
- Today's move: today's change against the average daily move of the last 20 sessions.

Each extreme votes down or up; the sign of the votes is the direction the price has gone
too far in. The scanner leans against it (after a drop it sells more put-side premium).
"""

import asyncio
import logging
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
HISTORY_DAYS = 370
AVG_SESSIONS = 20


def compute(
    closes: list[tuple[date, float]],
    price: float | None,
    change_pct: float | None,
    iv30: float | None,
    s: dict,
) -> dict | None:
    """The three measures and the direction. s holds the scanner's thresholds:
    range_band (share), move_days (sessions), move_sigma, day_ratio."""
    values = [c for _, c in closes if c and c > 0]
    if not price or len(values) < 2:
        return None
    out: dict = {
        "reasons": [],
        "votes": 0,
        "range_pos": None,
        "high": None,
        "low": None,
        "move_z": None,
        "move_days": s["move_days"],
        "day_ratio": None,
    }
    votes = []
    hi, lo = max(values + [price]), min(values + [price])
    out["high"], out["low"] = round(hi, 2), round(lo, 2)
    if hi > lo:
        pos = (price - lo) / (hi - lo)
        out["range_pos"] = round(pos, 3)
        if pos <= s["range_band"]:
            votes.append(-1)
            gap = price / lo - 1
            out["reasons"].append(
                "at the 52-week low" if gap < 0.005 else f"within {gap:.0%} of the 52-week low"
            )
        elif pos >= 1 - s["range_band"]:
            votes.append(1)
            gap = 1 - price / hi
            out["reasons"].append(
                "at the 52-week high" if gap < 0.005 else f"within {gap:.0%} of the 52-week high"
            )
    n = int(s["move_days"])
    if iv30 and iv30 > 0 and len(values) >= n:
        ref = values[-n]
        sigma = iv30 / 100 * math.sqrt(n / 252)
        z = math.log(price / ref) / sigma
        out["move_z"] = round(z, 2)
        if abs(z) >= s["move_sigma"]:
            votes.append(1 if z > 0 else -1)
            out["reasons"].append(f"{n}-day move {z:+.1f}σ")
    returns = [
        abs(math.log(b / a))
        for a, b in zip(values[-AVG_SESSIONS - 1 : -1], values[-AVG_SESSIONS:], strict=False)
    ]
    if change_pct is None and values:
        change_pct = (price / values[-1] - 1) * 100
    if returns and change_pct is not None:
        avg = sum(returns) / len(returns) * 100
        if avg > 0:
            ratio = abs(change_pct) / avg
            out["day_ratio"] = round(ratio, 2)
            if ratio >= s["day_ratio"]:
                votes.append(1 if change_pct > 0 else -1)
                out["reasons"].append(f"today {change_pct:+.1f}%, {ratio:.1f}× the average move")
    total = sum(votes)
    out["votes"] = total
    out["direction"] = "down" if total < 0 else "up" if total > 0 else None
    out["extreme"] = bool(votes)
    return out


class PriceHistory:
    """A year of daily closes per symbol, read from Schwab once a day."""

    def __init__(self, store, source, symbols, clock=lambda: datetime.now(ET)):
        self.store = store
        self.source = source
        self.symbols = symbols  # async () -> set of symbols to keep
        self.clock = clock
        self.closes: dict[str, list[tuple[date, float]]] = {}
        self.fetched: dict[str, date] = {}
        self.lock = asyncio.Lock()

    def get(self, symbol: str) -> list[tuple[date, float]] | None:
        return self.closes.get(symbol)

    async def ensure(self, symbol: str) -> list[tuple[date, float]] | None:
        """Closes for a symbol, fetched if not yet today. Falls back to the stored copy."""
        today = self.clock().astimezone(ET).date()
        if self.fetched.get(symbol) == today:
            return self.closes.get(symbol)
        async with self.lock:
            if self.fetched.get(symbol) == today:
                return self.closes.get(symbol)
            try:
                closes = await self.source.daily_closes(symbol, HISTORY_DAYS)
                if closes:
                    await self.store.upsert_prices(symbol, closes)
            except Exception as e:
                log.info("Price history for %s not read: %s", symbol, e)
                stored = (await self.store.prices([symbol])).get(symbol) or {}
                since = today - timedelta(days=HISTORY_DAYS)
                closes = sorted((d, v) for d, v in stored.items() if d >= since)
            self.fetched[symbol] = today
            # Today's own bar, if Schwab already has one, is not a previous close.
            self.closes[symbol] = [(d, v) for d, v in sorted(closes) if d < today]
            return self.closes[symbol]

    async def run(self) -> None:
        while True:
            try:
                for sym in sorted(await self.symbols()):
                    await self.ensure(sym)
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Price history refresh failed")
            await asyncio.sleep(3600)
