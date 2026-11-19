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


async def run_in_background(fn) -> None:
    """Runs a background service's loop with its requests paced."""
    with background():
        await fn()


class Pacer:
    """At most `per_window` acquisitions in any `window` seconds; callers wait their turn."""

    def __init__(self, per_window: int = BACKGROUND_PER_MIN, window: float = WINDOW_S, clock=None):
        self.per_window = per_window
        self.window = window
        self.clock = clock or time.monotonic
        self.sent: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:  # first come, first served
            while True:
                now = self.clock()
                while self.sent and now - self.sent[0] >= self.window:
                    self.sent.popleft()
                if len(self.sent) < self.per_window:
                    self.sent.append(now)
                    return
                await asyncio.sleep(self.window - (now - self.sent[0]) + 0.01)
