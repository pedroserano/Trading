"""Schwab's streamer: live quotes and account activity over one WebSocket.

Schwab allows one streamer connection per user. This service owns it, subscribes to what
the portfolio and watchlist need, and merges updates into the shared quote table. When the
stream is down, the portfolio keeps polling REST quotes, so streaming only ever makes data
fresher; it is never required.
"""

import asyncio
import logging
import time
from collections.abc import Callable

from .options import parse_option_symbol

log = logging.getLogger(__name__)

# Streamer field names (schwab-py labels) -> the REST quote names the rest of the app uses.
OPTION_FIELDS = {
    "BID_PRICE": "bidPrice",
    "ASK_PRICE": "askPrice",
    "LAST_PRICE": "lastPrice",
    "MARK": "mark",
    "DELTA": "delta",
    "GAMMA": "gamma",
    "THETA": "theta",
    "VEGA": "vega",
    "VOLATILITY": "volatility",
    "UNDERLYING_PRICE": "underlyingPrice",
    "NET_PERCENT_CHANGE": "netPercentChange",
    "OPEN_INTEREST": "openInterest",
}
EQUITY_FIELDS = {
    "BID_PRICE": "bidPrice",
    "ASK_PRICE": "askPrice",
    "LAST_PRICE": "lastPrice",
    "MARK": "mark",
    "CLOSE_PRICE": "closePrice",
    "NET_CHANGE_PERCENT": "netPercentChange",
}

FRESH_S = 15.0  # a streamed quote younger than this needs no REST poll


def normalise(content: dict, mapping: dict) -> dict:
    out = {}
    for k, v in content.items():
        name = mapping.get(k)
        if name is not None and v is not None:
            out[name] = v
    return out


class StreamService:
    def __init__(
        self,
        schwab,
        wanted: Callable[[], set[str]],
        on_quotes: Callable[[dict[str, dict]], None],
        on_account_activity: Callable[[], None],
        client_factory=None,
    ):
        self.schwab = schwab
        self.wanted = wanted
        self.on_quotes = on_quotes
        self.on_account_activity = on_account_activity
        self.client_factory = client_factory or self._default_factory
        self.status = "off"
        self.detail: str | None = None
        self.updated: dict[str, float] = {}
        self._subscribed_eq: set[str] = set()
        self._subscribed_opt: set[str] = set()
        self._client = None

    @staticmethod
    def _default_factory(client):
        from schwab.streaming import StreamClient

        return StreamClient(client)

    def fresh(self, symbol: str, now: float | None = None) -> bool:
        t = self.updated.get(symbol)
        return t is not None and (now or time.time()) - t < FRESH_S

    # Handlers ----------------------------------------------------------------------

    def _handle(self, msg: dict, mapping: dict) -> None:
        now = time.time()
        batch = {}
        for item in msg.get("content", []):
            key = item.get("key")
            if not key:
                continue
            fields = normalise(item, mapping)
            if fields:
                batch[key] = fields
                self.updated[key] = now
        if batch:
            self.on_quotes(batch)

    def handle_option(self, msg: dict) -> None:
        self._handle(msg, OPTION_FIELDS)

    def handle_equity(self, msg: dict) -> None:
        self._handle(msg, EQUITY_FIELDS)

    def handle_account(self, msg: dict) -> None:
        log.info("Account activity received; refreshing positions")
        self.on_account_activity()

    # Subscriptions -----------------------------------------------------------------

    async def sync(self) -> None:
        want = self.wanted()
        opts = {s for s in want if parse_option_symbol(s)}
        eqs = {s for s in want - opts if not s.startswith("$")}
        c = self._client
        if eqs != self._subscribed_eq:
            if eqs:
                await c.level_one_equity_subs(sorted(eqs))
            elif self._subscribed_eq:
                await c.level_one_equity_unsubs(sorted(self._subscribed_eq))
            self._subscribed_eq = eqs
        if opts != self._subscribed_opt:
            if opts:
                await c.level_one_option_subs(sorted(opts))
            elif self._subscribed_opt:
                await c.level_one_option_unsubs(sorted(self._subscribed_opt))
            self._subscribed_opt = opts

    # Loop ----------------------------------------------------------------------------

    async def run(self) -> None:
        backoff = 10
        while True:
            if not self.schwab.status().connected:
                self.status, self.detail = "off", "Schwab not connected"
                await asyncio.sleep(10)
                continue
            try:
                await self._session()
                backoff = 10
            except asyncio.CancelledError:
                raise
            except Exception as e:
                text = f"{type(e).__name__}: {e}"
                self.status, self.detail = "down", text[:300]
                # Code 12: Schwab allows one streamer per user and closed this one.
                long_wait = "CLOSE_CONNECTION" in text or "maximum number" in text
                wait = 300 if long_wait else backoff
                log.warning("Stream stopped (%s); retrying in %ss", text, wait)
                backoff = min(backoff * 2, 300)
                await asyncio.sleep(wait)
            finally:
                self._client = None
                self._subscribed_eq, self._subscribed_opt = set(), set()

    async def _session(self) -> None:
        self.status, self.detail = "connecting", None
        client = self.client_factory(self.schwab.client())
        self._client = client
        await client.login()
        client.add_level_one_option_handler(self.handle_option)
        client.add_level_one_equity_handler(self.handle_equity)
        client.add_account_activity_handler(self.handle_account)
        await client.account_activity_sub()
        self.status = "live"
        log.info("Streaming connected")
        while True:
            await self.sync()
            try:
                await asyncio.wait_for(client.handle_message(), timeout=5)
            except TimeoutError:
                pass
