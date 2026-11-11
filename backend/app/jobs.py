"""Daily background work: record each underlying's 30-day IV, refresh betas."""

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import analytics
from .portfolio import BENCH
from .schwab_service import SchwabError, SchwabNotConnected

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
IV_RECORD_AFTER = (16, 20)  # ET, after the options close
BETA_MAX_AGE_DAYS = 7


class DailyJobs:
    def __init__(self, source, store, portfolio, settings):
        self.source = source
        self.store = store
        self.portfolio = portfolio
        self.settings = settings
        self.last_run: dict = {}

    def symbols(self) -> list[str]:
        held = {leg.underlying for leg in self.portfolio.legs}
        return sorted(held | set(self.portfolio.watchlist) | {BENCH})

    async def run(self) -> None:
        while True:
            try:
                await self.run_once(datetime.now(ET))
            except asyncio.CancelledError:
                raise
            except (SchwabNotConnected, SchwabError) as e:
                log.info("Daily jobs skipped: %s", e)
            except Exception:
                log.exception("Daily jobs failed")
            await asyncio.sleep(600)

    async def run_once(self, now: datetime) -> None:
        await self.refresh_betas(now.date())
        if (now.hour, now.minute) >= IV_RECORD_AFTER and now.weekday() < 5:
            done = await self.store.get_setting("iv_recorded_on")
            if done != now.date().isoformat():
                if await self.source.market_open(now.date()) is not False:
                    await self.record_iv(now.date())
                await self.store.set_setting("iv_recorded_on", now.date().isoformat())

    async def record_iv(self, today: date) -> dict:
        results = {}
        for sym in self.symbols():
            try:
                chain = await self.source.option_chain(
                    sym, today + timedelta(days=7), today + timedelta(days=70)
                )
                value = analytics.iv30(analytics.atm_iv_by_expiration(chain or {}))
                if value is not None:
                    await self.store.upsert_iv(sym, today, round(value, 3), "schwab-chain")
                results[sym] = value
            except SchwabError as e:
                log.warning("IV for %s not recorded: %s", sym, e)
                results[sym] = None
        log.info("Recorded IV30: %s", results)
        self.last_run["iv"] = (today.isoformat(), results)
        return results

    async def refresh_betas(self, today: date) -> None:
        stale = []
        for sym in self.symbols():
            if sym == BENCH:
                continue
            b = await self.store.get_beta(sym)
            if b is None or (today - b[1]).days >= BETA_MAX_AGE_DAYS:
                stale.append(sym)
        if not stale:
            return
        bench = await self.source.daily_closes(BENCH, 370)
        for sym in stale:
            closes = await self.source.daily_closes(sym, 370)
            value = analytics.beta(closes, bench)
            if value is not None:
                await self.store.upsert_beta(sym, round(value, 3), today)
