"""Pacing for background market-data requests to Schwab.

Schwab allows about 120 market-data requests a minute. What a person or the account's own
upkeep asks for (quotes, positions, orders, the Trade screen's chain, automanage's rolls) goes
straight through; bulk work in the background (the scanner, the watchlist's chains, price
histories, fundamentals) is held to BACKGROUND_PER_MIN, so a scan over a long watchlist queues
instead of running into Schwab's limit and failing with 429s.
"""

import asyncio
import contextlib
import contextvars
import time
from collections import deque

# Background requests a minute. Foreground polling (quotes for each account, positions, the
# market strip) stays well under the other half of Schwab's ~120.
BACKGROUND_PER_MIN = 60
WINDOW_S = 60.0

_background: contextvars.ContextVar[bool] = contextvars.ContextVar("background", default=False)
# Background work someone is waiting for (a scan): ahead of the routine refreshes in the queue.
_priority: contextvars.ContextVar[bool] = contextvars.ContextVar("priority", default=False)
# Seconds spent waiting for the limit, added up for whoever set a counter (the scanner).
_waited: contextvars.ContextVar[list | None] = contextvars.ContextVar("waited", default=None)


def is_background() -> bool:
    return _background.get()


@contextlib.contextmanager
def background():
    """Marks the requests made inside (and in tasks started inside) as background work."""
    token = _background.set(True)
    try:
        yield
    finally:
        _background.reset(token)


def is_priority() -> bool:
    return _priority.get()


@contextlib.contextmanager
def priority():
    """Background requests made inside go ahead of other background requests."""
    token = _priority.set(True)
    try:
        yield
    finally:
        _priority.reset(token)


@contextlib.contextmanager
def count_waits(into: list):
    """Adds the seconds each paced request inside waited, and how many there were, into
    `into` ([seconds, requests])."""
    token = _waited.set(into)
    try:
        yield
    finally:
        _waited.reset(token)


async def run_in_background(fn) -> None:
    """Runs a background service's loop with its requests paced."""
    with background():
        await fn()


class Pacer:
    """At most `per_window` acquisitions in any `window` seconds; callers wait their turn,
    priority callers first."""

    def __init__(self, per_window: int = BACKGROUND_PER_MIN, window: float = WINDOW_S, clock=None):
        self.per_window = per_window
        self.window = window
        self.clock = clock or time.monotonic
        self.sent: deque[float] = deque()
        self.lock = asyncio.Lock()
        self.urgent = 0  # priority callers waiting

    async def acquire(self, priority: bool | None = None) -> None:
        if priority is None:
            priority = is_priority()
        start = self.clock()
        if priority:
            self.urgent += 1
        try:
            if priority:
                await self._take()
            else:
                async with self.lock:  # first come, first served among the rest
                    await self._take(yield_to_urgent=True)
        finally:
            if priority:
                self.urgent -= 1
            into = _waited.get()
            if into is not None:
                into[0] += self.clock() - start
                into[1] += 1

    async def _take(self, yield_to_urgent: bool = False) -> None:
        while True:
            now = self.clock()
            while self.sent and now - self.sent[0] >= self.window:
                self.sent.popleft()
            if yield_to_urgent and self.urgent:
                await asyncio.sleep(0.2)
                continue
            if len(self.sent) < self.per_window:
                self.sent.append(now)
                return
            await asyncio.sleep(self.window - (now - self.sent[0]) + 0.01)
