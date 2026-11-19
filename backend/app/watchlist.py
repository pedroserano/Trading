"""Live numbers for the watchlist screen: IV, IV rank, expected move, liquidity.

Each symbol's option chain is fetched at most every 15 minutes; prices come from the
portfolio's live quotes.
"""

import asyncio
import logging
import math
import time
from datetime import date, timedelta

from . import analytics, ivref
from .chain import atm_liquidity, chain_rows, default_expiration, expirations
from .portfolio import BENCH, MIN_IVR_DAYS
from .schwab_service import SchwabError, SchwabNotConnected
from .signals import skew

log = logging.getLogger(__name__)
CHAIN_REFRESH_S = 15 * 60
# Option chains fetched per 10-second pass, at most: about 60 a minute, well inside Schwab's
# 120 requests a minute with room for quotes and orders, however long the watchlist. A list
# longer than about 900 symbols refreshes less often than every 15 minutes.
CHAINS_PER_PASS = 10
# Float and shares outstanding change rarely: fetched once a day.
FUNDAMENTALS_REFRESH_S = 24 * 3600
# Under this many shares in the float counts as a low float.
LOW_FLOAT_SHARES = 20_000_000


class WatchlistService:
    def __init__(self, source, store, portfolio, preferences=None):
        self.preferences = preferences
        self.source = source
        self.store = store
        self.portfolio = portfolio
        self.cache: dict[str, dict] = {}
        self.fundamentals: dict[str, dict] = {}  # symbol -> {"at", "float": {...} or None}
        self.earnings = None  # symbol -> next earnings report, set by the app
        self.extremes = None  # async (symbol, price, change %, iv30) -> price extremes
        self.shock = None  # async (symbol, change %) -> why it may have had a shock, or None
        self.ivref = None  # symbol -> DoltHub's IV rank, until our own history spans a year

    async def run(self) -> None:
        while True:
            try:
                await self.refresh(self.portfolio.clock().date())
            except asyncio.CancelledError:
                raise
            except (SchwabNotConnected, SchwabError) as e:
                log.info("Watchlist refresh skipped: %s", e)
            except Exception:
                log.exception("Watchlist refresh failed")
            # Cheap when nothing is due: only symbols older than CHAIN_REFRESH_S are fetched.
            await asyncio.sleep(10)

    async def refresh(self, today: date, force: bool = False) -> None:
        now = time.time()
        await self._refresh_fundamentals(now, force)
        fetched = 0
        # Never fetched first, then the oldest.
        order = sorted(
            self.portfolio.watchlist, key=lambda s: (self.cache.get(s) or {}).get("at", 0)
        )
        for sym in order:
            entry = self.cache.get(sym)
            if not force and entry and now - entry["at"] < CHAIN_REFRESH_S:
                continue
            if not force and fetched >= CHAINS_PER_PASS:
                break
            fetched += 1
            try:
                self.cache[sym] = {"at": now, **await self.chain_stats(sym, today)}
            except SchwabError as e:
                self.cache[sym] = {"at": now, "error": str(e)[:200]}

    async def _refresh_fundamentals(self, now: float, force: bool) -> None:
        due = [
            sym
            for sym in self.portfolio.watchlist
            # Indexes ($SPX) have no shares.
            if not sym.startswith("$")
            and (
                force
                or now - self.fundamentals.get(sym, {}).get("at", -1e12) >= FUNDAMENTALS_REFRESH_S
            )
        ]
        if not due or not hasattr(self.source, "fundamentals"):
            return
        try:
            raw = await self.source.fundamentals(due)
        except SchwabError as e:
            log.info("Fundamentals skipped: %s", e)
            # Try again at the next chain refresh rather than every 10 seconds.
            for sym in due:
                self.fundamentals[sym] = {
                    **self.fundamentals.get(sym, {}),
                    "at": now - FUNDAMENTALS_REFRESH_S + CHAIN_REFRESH_S,
                }
            return
        for sym in due:
            self.fundamentals[sym] = {"at": now, "float": float_info(raw.get(sym))}

    async def float_of(self, sym: str) -> dict | None:
        """One symbol's float, from the daily cache or fetched now: for the order ticket,
        which can trade symbols not on the watchlist. None when Schwab has none or fails."""
        now = time.time()
        hit = self.fundamentals.get(sym)
        if hit and "float" in hit and now - hit["at"] < FUNDAMENTALS_REFRESH_S:
            return hit["float"]
        if sym.startswith("$") or not hasattr(self.source, "fundamentals"):
            return None
        try:
            raw = await self.source.fundamentals([sym])
        except (SchwabError, SchwabNotConnected) as e:
            log.info("Float for %s skipped: %s", sym, e)
            return (hit or {}).get("float")
        self.fundamentals[sym] = {"at": now, "float": float_info(raw.get(sym))}
        return self.fundamentals[sym]["float"]

    async def chain_stats(self, sym: str, today: date) -> dict:
        raw = await self.source.option_chain(
            sym, today + timedelta(days=5), today + timedelta(days=75)
        )
        iv30 = analytics.iv30(analytics.atm_iv_by_expiration(raw or {}))
        # Liquidity and expected move at the expiration the trade screen would default to.
        dates = sorted({k.split(":")[0] for k in (raw or {}).get("putExpDateMap", {})})
        exps = expirations({"expirationList": [{"expirationDate": d} for d in dates]}, today)
        target = default_expiration(exps)
        stats = {"iv30": None if iv30 is None else round(iv30, 2), "expiration": target}
        if target:
            sub = {
                "underlyingPrice": (raw or {}).get("underlyingPrice"),
                **{
                    m: {k: v for k, v in (raw or {}).get(m, {}).items() if k.startswith(target)}
                    for m in ("callExpDateMap", "putExpDateMap")
                },
            }
            rows = chain_rows(sub)
            stats["dte"] = rows["dte"]
            stats["expected_move"] = rows["expected_move"]
            stats["skew"] = skew(rows["rows"])
            liq = atm_liquidity(rows["rows"], rows["underlying_price"])
            stats["spread_pct"] = liq["spread_pct"]
            stats["spread_width"] = liq["width"]
            stats["atm_oi"] = liq["oi"]
        return stats

    async def rows(self) -> list[dict]:
        held = self._held()
        since = self.portfolio.clock().date() - timedelta(days=366)
        # The saved list is the truth; the portfolio's copy can lag by one refresh.
        symbols = (
            await self.preferences.watchlist() if self.preferences else self.portfolio.watchlist
        )
        return [await self._row(sym, held, since) for sym in symbols]

    async def row(self, symbol: str) -> dict | None:
        """One symbol's row, without building the rest: for previews, chains and the scanner."""
        symbols = (
            await self.preferences.watchlist() if self.preferences else self.portfolio.watchlist
        )
        if symbol not in symbols:
            return None
        since = self.portfolio.clock().date() - timedelta(days=366)
        return await self._row(symbol, self._held(), since)

    def _held(self) -> dict:
        return {u["symbol"]: u for u in self.portfolio.snapshot.get("underlyings", [])}

    async def _row(self, sym: str, held: dict, since) -> dict:
        p = self.portfolio
        q = p._quote(sym)
        c = self.cache.get(sym, {})
        if sym == BENCH:
            ivr = (p.snapshot.get("ivr") or {}).get("SPY")
        else:
            dated = await self.store.iv_history(sym, since)
            hist = [v for _, v in dated]
            current = c.get("iv30") or (hist[-1] if hist else None)
            ivr = analytics.iv_rank(current, hist) if hist else None
            if ivr:
                ivr["source"] = "recorded"
                if ivr["days"] < MIN_IVR_DAYS:
                    ivr["rank"] = ivr["percentile"] = None
            ref = self.ivref(sym) if self.ivref else None
            ivr = ivref.choose(ivr, dated[0][0] if dated else None, ref, p.clock().date())
        u = held.get(sym)
        change = _num(q.get("netPercentChange"))
        iv30 = c.get("iv30")
        if iv30 is None and sym == BENCH:
            iv30 = p._price("$VIX")
        ext = await self.extremes(sym, p._price(sym), change, iv30) if self.extremes else None
        return {
            "symbol": sym,
            "price": p._price(sym),
            "change_pct": change,
            "iv30": c.get("iv30"),
            "ivr": ivr,
            "expected_move": c.get("expected_move"),
            "expiration": c.get("expiration"),
            "dte": c.get("dte"),
            "spread_pct": c.get("spread_pct"),
            "atm_oi": c.get("atm_oi"),
            "spread_width": c.get("spread_width"),
            "liquidity": analytics.liquidity_stars(
                c.get("spread_pct"), c.get("spread_width"), c.get("atm_oi")
            ),
            "earnings": self.earnings(sym) if self.earnings else None,
            "extreme": ext,
            "skew": c.get("skew"),
            "float": self.fundamentals.get(sym, {}).get("float"),
            "shock": await self.shock(sym, change) if self.shock else None,
            "error": c.get("error"),
            "held": u is not None,
            "position_delta": None if u is None else u["delta"],
            "position_theta": None if u is None else u["theta"],
        }


def float_info(f: dict | None) -> dict | None:
    """The float in shares, its share of all shares, and how much of it trades a day.

    None for funds and indexes, which Schwab gives no float for.
    """
    f = f or {}
    shares = _num(f.get("marketCapFloat"))
    if not shares or shares <= 0:
        return None
    outstanding = _num(f.get("sharesOutstanding"))
    volume = _num(f.get("avg10DaysVolume"))
    return {
        "shares": shares,
        "pct_of_outstanding": shares / outstanding if outstanding and outstanding > 0 else None,
        "turnover": volume / shares if volume else None,
        "low": shares < LOW_FLOAT_SHARES,
    }


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None
