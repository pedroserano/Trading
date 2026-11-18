"""API keys for AI and script access.

A key is a bearer token for /api/* and /mcp. Only its SHA-256 hash is stored; the key itself
is shown once, when it is made. Each key has a level:

  read  GET routes, plus the read-only POSTs (analyze, preview, order check)
  sim   also trades on the simulated account
  live  also trades on Schwab accounts, through the same live gate as a manual order

A key can never change settings, rules, views, keys or the Schwab connection: those routes
stay with the signed-in session.
"""

import hashlib
import hmac
import re
import secrets
import time

KEYS_SETTING = "api.keys"
PREFIX = "trd_"
LEVELS = ("read", "sim", "live")
LEVEL_NAMES = {
    "read": "Read only",
    "sim": "Read + simulated trades",
    "live": "Read + simulated and live trades",
}
MAX_KEYS = 20
NAME_RE = re.compile(r"^[\w .\-]{1,40}$")
# How often last_used is written back, so a busy key doesn't write on every call.
TOUCH_EVERY_S = 60

# POSTs any key may make: they read or estimate, and change nothing.
READ_POSTS = (
    re.compile(r"^/api/analyze$"),
    re.compile(r"^/api/orders/preview$"),
    re.compile(r"^/api/order-check/refresh$"),
)
# POSTs that trade: the key's level must cover the account they name.
TRADE_POSTS = (
    re.compile(r"^/api/orders$"),
    re.compile(r"^/api/orders/[^/]+/cancel$"),
    re.compile(r"^/api/automation/trade$"),
    re.compile(r"^/api/automation/proposal/[^/]+/(accept|dismiss)$"),
    re.compile(r"^/api/scanner/run$"),
)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def route_kind(method: str, path: str) -> str:
    """'read', 'trade' or 'refused': what a key needs for this /api route."""
    if method in ("GET", "HEAD"):
        return "read"
    if any(p.match(path) for p in READ_POSTS):
        return "read"
    if any(p.match(path) for p in TRADE_POSTS):
        return "trade"
    return "refused"


def allows(level: str, kind: str, account: str) -> bool:
    """Whether a key at level may make a call of kind on account."""
    if kind == "read":
        return level in LEVELS
    if kind == "trade":
        return level == "live" or (level == "sim" and account == "sim")
    return False


class ApiKeys:
    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock

    async def all(self) -> list[dict]:
        return list(await self.store.get_setting(KEYS_SETTING) or [])

    async def _save(self, keys: list[dict]) -> None:
        await self.store.set_setting(KEYS_SETTING, keys)

    async def create(self, name: str, level: str) -> tuple[str, dict]:
        """Make a key. Returns (the key, shown once; its stored record)."""
        name = name.strip()
        if not NAME_RE.match(name):
            raise ValueError("Name the key with up to 40 letters, digits, spaces, dots or dashes")
        if level not in LEVELS:
            raise ValueError(f"Level must be one of {', '.join(LEVELS)}")
        keys = await self.all()
        if len(keys) >= MAX_KEYS:
            raise ValueError(f"At most {MAX_KEYS} keys; revoke one first")
        if any(k["name"] == name for k in keys):
            raise ValueError(f"A key named {name!r} already exists")
        key = PREFIX + secrets.token_urlsafe(32)
        rec = {
            "id": secrets.token_hex(6),
            "name": name,
            "level": level,
            "hash": _hash(key),
            "last4": key[-4:],
            "created": int(self.clock()),
            "last_used": None,
        }
        await self._save([*keys, rec])
        return key, rec

    async def revoke(self, key_id: str) -> bool:
        keys = await self.all()
        left = [k for k in keys if k["id"] != key_id]
        if len(left) == len(keys):
            return False
        await self._save(left)
        return True

    async def check(self, key: str) -> dict | None:
        """The record for a presented key, or None."""
        if not key.startswith(PREFIX):
            return None
        h = _hash(key)
        keys = await self.all()
        found = None
        for k in keys:
            if hmac.compare_digest(k["hash"], h):
                found = k
        if found is None:
            return None
        now = int(self.clock())
        if not found.get("last_used") or now - found["last_used"] >= TOUCH_EVERY_S:
            found["last_used"] = now
            await self._save(keys)
        return found
