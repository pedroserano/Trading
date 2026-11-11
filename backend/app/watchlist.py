"""Live numbers for the watchlist screen: IV, IV rank, expected move, liquidity.

Each symbol's option chain is fetched at most every 15 minutes; prices come from the
portfolio's live quotes.
"""

import asyncio
import logging
import math
import time
from datetime import date, timedelta

from . import analytics
from .chain import chain_rows, default_expiration, expirations
from .portfolio import BENCH, MIN_IVR_DAYS
from .schwab_service import SchwabError, SchwabNotConnected

log = logging.getLogger(__name__)
CHAIN_REFRESH_S = 15 * 60


class WatchlistService:
    def __init__(self, source, store, portfolio):
        self.source = source
        self.store = store
        self.portfolio = portfolio
        self.cache: dict[str, dict] = {}

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
        for sym in self.portfolio.watchlist:
            entry = self.cache.get(sym)
            if not force and entry and now - entry["at"] < CHAIN_REFRESH_S:
                continue
            try:
                self.cache[sym] = {"at": now, **await self.chain_stats(sym, today)}
            except SchwabError as e:
                self.cache[sym] = {"at": now, "error": str(e)[:200]}

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
            spot = rows["underlying_price"]
            if spot and rows["rows"]:
                atm = min(rows["rows"], key=lambda r: abs(r["strike"] - spot))
                spreads, ois = [], []
                for c in (atm["call"], atm["put"]):
                    if c and c["bid"] is not None and c["ask"] and c["mark"]:
                        spreads.append((c["ask"] - c["bid"]) / c["mark"])
                    if c and c["oi"] is not None:
                        ois.append(c["oi"])
                stats["spread_pct"] = round(sum(spreads) / len(spreads), 4) if spreads else None
                stats["atm_oi"] = int(sum(ois)) if ois else None
        return stats

    async def rows(self) -> list[dict]:
        p = self.portfolio
        held = {u["symbol"]: u for u in p.snapshot.get("underlyings", [])}
        since = p.clock().date() - timedelta(days=366)
        out = []
        for sym in p.watchlist:
            q = p._quote(sym)
            c = self.cache.get(sym, {})
            if sym == BENCH:
                ivr = (p.snapshot.get("ivr") or {}).get("SPY")
            else:
                hist = [v for _, v in await self.store.iv_history(sym, since)]
                current = c.get("iv30") or (hist[-1] if hist else None)
                ivr = analytics.iv_rank(current, hist) if hist else None
                if ivr:
                    ivr["source"] = "recorded"
                    if ivr["days"] < MIN_IVR_DAYS:
                        ivr["rank"] = ivr["percentile"] = None
            u = held.get(sym)
            out.append(
                {
                    "symbol": sym,
                    "price": p._price(sym),
                    "change_pct": _num(q.get("netPercentChange")),
                    "iv30": c.get("iv30"),
                    "ivr": ivr,
                    "expected_move": c.get("expected_move"),
                    "expiration": c.get("expiration"),
                    "dte": c.get("dte"),
                    "spread_pct": c.get("spread_pct"),
                    "atm_oi": c.get("atm_oi"),
                    "error": c.get("error"),
                    "held": u is not None,
                    "position_delta": None if u is None else u["delta"],
                    "position_theta": None if u is None else u["theta"],
                }
            )
        return out


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None
