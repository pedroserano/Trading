"""Where account and market data come from: Schwab."""

import asyncio
import time
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from .pacing import is_background
from .schwab_service import SchwabService

VIX = "$VIX"


class DataSource(Protocol):
    async def account_numbers(self) -> list[dict]: ...
    async def account(self, account_hash: str) -> dict: ...
    async def quotes(self, symbols: list[str]) -> dict: ...
    async def daily_closes(self, symbol: str, days: int) -> list[tuple[date, float]]: ...
    async def daily_candles(self, symbol: str, days: int) -> list[dict]: ...
    async def option_chain(
        self, symbol: str, from_date: date, to_date: date, strike_count: int | None = 6
    ) -> dict: ...
    async def expirations(self, symbol: str) -> dict: ...
    async def transactions(self, account_hash: str, start: datetime, end: datetime) -> list: ...
    async def market_open(self, day: date) -> bool | None: ...
    async def orders(self, account_hash: str, start: datetime, end: datetime) -> list: ...
    async def fundamentals(self, symbols: list[str]) -> dict: ...


# Quotes fetched within this many seconds are reused: every account's portfolio asks for
# SPY, VIX and the watchlist on the same few-second cycle.
QUOTE_TTL_S = 2.0
# Background chains (the scanner, the watchlist) are shared for this long: each account's scan
# asks for the same symbols minutes apart. Anything a person or automanage asks for is fresh.
CHAIN_TTL_S = 180.0
CHAIN_CACHE_MAX = 150
# Expirations change at most once a day (new weeklies are listed overnight).
EXPIRATIONS_TTL_S = 1800.0


class SchwabDataSource:
    def __init__(self, schwab: SchwabService):
        self.schwab = schwab
        self._quotes: dict[str, tuple[float, dict]] = {}
        self._quotes_lock = asyncio.Lock()
        self._chains: dict[tuple, tuple[float, dict]] = {}
        self._expirations: dict[str, tuple[float, dict]] = {}

    async def account_numbers(self) -> list[dict]:
        return await self.schwab.call("get_account_numbers") or []

    async def account_names(self) -> dict[str, str]:
        """Each account's name in Schwab (its nickname), by account number."""
        raw = await self.schwab.call("get_user_preferences")
        prefs = raw[0] if isinstance(raw, list) and raw else raw or {}
        return {
            str(a.get("accountNumber")): (a.get("nickName") or "").strip()
            for a in (prefs.get("accounts") or [])
            if a.get("accountNumber")
        }

    async def account(self, account_hash: str) -> dict:
        c = self.schwab.client()
        return await self.schwab.call(
            "get_account", account_hash, fields=[c.Account.Fields.POSITIONS]
        )

    async def quotes(self, symbols: list[str]) -> dict:
        # One request at a time, so a second account asking at the same moment is served
        # from what the first just fetched.
        async with self._quotes_lock:
            now = time.monotonic()
            missing = [
                s for s in symbols if now - self._quotes.get(s, (-1e9, None))[0] > QUOTE_TTL_S
            ]
            # Keep each request modest in size.
            for i in range(0, len(missing), 200):
                got = await self.schwab.call("get_quotes", missing[i : i + 200]) or {}
                for sym, q in got.items():
                    self._quotes[sym] = (now, q)
            if len(self._quotes) > 2000:
                self._quotes = {k: v for k, v in self._quotes.items() if now - v[0] <= QUOTE_TTL_S}
        # Copies: callers keep and update what they get (streamed fields land in "quote").
        out = {}
        for s in symbols:
            hit = self._quotes.get(s)
            if hit is not None:
                q = hit[1]
                out[s] = (
                    {**q, "quote": dict(q["quote"])}
                    if isinstance(q.get("quote"), dict)
                    else dict(q)
                )
        return out

    async def daily_candles(self, symbol: str, days: int) -> list[dict]:
        """Daily bars, oldest first: {date, open, high, low, close, volume}."""
        start = datetime.now(UTC) - timedelta(days=days)
        raw = await self.schwab.call("get_price_history_every_day", symbol, start_datetime=start)
        out = []
        for c in (raw or {}).get("candles", []):
            out.append(
                {
                    "date": datetime.fromtimestamp(c["datetime"] / 1000, UTC).date(),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "volume": int(c.get("volume") or 0),
                }
            )
        return out

    async def daily_closes(self, symbol: str, days: int) -> list[tuple[date, float]]:
        start = datetime.now(UTC) - timedelta(days=days)
        raw = await self.schwab.call("get_price_history_every_day", symbol, start_datetime=start)
        out = []
        for c in (raw or {}).get("candles", []):
            d = datetime.fromtimestamp(c["datetime"] / 1000, UTC).date()
            out.append((d, float(c["close"])))
        return out

    async def option_chain(
        self, symbol: str, from_date: date, to_date: date, strike_count: int | None = 6
    ) -> dict:
        """strike_count None returns every strike. Callers only read the answer."""
        shared = is_background()
        key = (symbol, from_date, to_date, strike_count)
        now = time.monotonic()
        if shared:
            hit = self._chains.get(key)
            if hit is not None and now - hit[0] <= CHAIN_TTL_S:
                return hit[1]
        c = self.schwab.client()
        got = await self.schwab.call(
            "get_option_chain",
            symbol,
            contract_type=c.Options.ContractType.ALL,
            strike_count=strike_count,
            from_date=from_date,
            to_date=to_date,
        )
        if shared and got:
            self._chains = {k: v for k, v in self._chains.items() if now - v[0] <= CHAIN_TTL_S}
            if len(self._chains) >= CHAIN_CACHE_MAX:
                self._chains.pop(min(self._chains, key=lambda k: self._chains[k][0]))
            self._chains[key] = (now, got)
        return got

    async def expirations(self, symbol: str) -> dict:
        now = time.monotonic()
        hit = self._expirations.get(symbol)
        if hit is not None and now - hit[0] <= EXPIRATIONS_TTL_S:
            return hit[1]
        got = await self.schwab.call("get_option_expiration_chain", symbol)
        if got:
            if len(self._expirations) > 2000:
                self._expirations = {
                    k: v for k, v in self._expirations.items() if now - v[0] <= EXPIRATIONS_TTL_S
                }
            self._expirations[symbol] = (now, got)
        return got

    async def fundamentals(self, symbols: list[str]) -> dict:
        """Schwab's fundamentals by symbol (shares outstanding, float, average volume).

        Its `marketCapFloat` is the float in shares, not dollars. Its short-interest fields
        come back as zero, so they are not used.
        """
        c = self.schwab.client()
        out = {}
        for i in range(0, len(symbols), 50):
            raw = await self.schwab.call(
                "get_instruments",
                symbols[i : i + 50],
                projection=c.Instrument.Projection.FUNDAMENTAL,
            )
            for inst in (raw or {}).get("instruments") or []:
                f = inst.get("fundamental") or {}
                sym = inst.get("symbol") or f.get("symbol")
                if sym:
                    out[sym] = f
        return out

    async def transactions(self, account_hash: str, start: datetime, end: datetime) -> list:
        c = self.schwab.client()
        types = [
            c.Transactions.TransactionType.TRADE,
            c.Transactions.TransactionType.RECEIVE_AND_DELIVER,
        ]
        return (
            await self.schwab.call(
                "get_transactions",
                account_hash,
                start_date=start,
                end_date=end,
                transaction_types=types,
            )
            or []
        )

    async def orders(self, account_hash: str, start: datetime, end: datetime) -> list:
        return (
            await self.schwab.call(
                "get_orders_for_account",
                account_hash,
                from_entered_datetime=start,
                to_entered_datetime=end,
                max_results=500,
            )
            or []
        )

    async def market_open(self, day: date) -> bool | None:
        c = self.schwab.client()
        raw = await self.schwab.call("get_market_hours", [c.MarketHours.Market.OPTION], date=day)
        try:
            markets = raw["option"]
            return any(m.get("isOpen") for m in markets.values())
        except (KeyError, TypeError, AttributeError):
            return None
