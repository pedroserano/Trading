"""Persistent state: settings, notification markers, IV history and betas.

`PgStore` is the real one. `MemoryStore` has the same interface for tests.
"""

import json
from datetime import UTC, date, datetime
from importlib import resources

from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool


class MemoryStore:
    def __init__(self):
        self.settings: dict[str, object] = {}
        self.notified: set[str] = set()
        self.iv: dict[str, dict[date, float]] = {}
        self.betas: dict[str, tuple[float, date]] = {}
        self.transactions_raw: dict[tuple[str, str], dict] = {}
        self.sheet: dict[str, tuple[str, dict]] = {}
        self.daily: dict[str, dict[date, float]] = {}

    async def upsert_sheet_rows(self, rows) -> dict:
        out = {"new": 0, "updated": 0, "unchanged": 0}
        for r in rows:
            old = self.sheet.get(r.key)
            out["new" if old is None else "unchanged" if old[1] == r.data else "updated"] += 1
            self.sheet[r.key] = (r.account, r.data)
        return out

    async def sheet_rows(self, account: str) -> list[tuple[str, dict]]:
        return [(k, d) for k, (a, d) in self.sheet.items() if a == account]

    async def sheet_accounts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a, _ in self.sheet.values():
            out[a] = out.get(a, 0) + 1
        return out

    async def upsert_prices(self, symbol: str, closes: list[tuple[date, float]]) -> None:
        self.daily.setdefault(symbol, {}).update(dict(closes))

    async def prices(self, symbols) -> dict[str, dict[date, float]]:
        return {s: dict(self.daily.get(s, {})) for s in symbols}

    async def upsert_transactions(self, items: list[dict], account: str = "live") -> int:
        new = 0
        for t in items:
            key = (account, str(t.get("activityId")))
            new += key not in self.transactions_raw
            self.transactions_raw[key] = t
        return new

    async def transactions(self, account: str = "live") -> list[dict]:
        return [t for (a, _), t in self.transactions_raw.items() if a == account]

    async def open(self):
        pass

    async def close(self):
        pass

    async def ping(self) -> str | None:
        return None

    async def get_setting(self, key: str):
        return self.settings.get(key)

    async def set_setting(self, key: str, value) -> None:
        self.settings[key] = value

    async def mark_notified(self, key: str) -> bool:
        if key in self.notified:
            return False
        self.notified.add(key)
        return True

    async def upsert_iv(self, symbol: str, day: date, iv30: float, source: str) -> None:
        self.iv.setdefault(symbol, {})[day] = iv30

    async def iv_history(self, symbol: str, since: date) -> list[tuple[date, float]]:
        return sorted((d, v) for d, v in self.iv.get(symbol, {}).items() if d >= since)

    async def upsert_beta(self, symbol: str, beta: float, as_of: date) -> None:
        self.betas[symbol] = (beta, as_of)

    async def get_beta(self, symbol: str) -> tuple[float, date] | None:
        return self.betas.get(symbol)


class PgStore:
    def __init__(self, host, port, dbname, user, password):
        self._conninfo = make_conninfo(
            host=host, port=port, dbname=dbname, user=user, password=password, connect_timeout=5
        )
        self._pool = AsyncConnectionPool(self._conninfo, min_size=1, max_size=4, open=False)

    async def open(self):
        await self._pool.open(wait=True, timeout=30)
        await self._migrate()

    async def close(self):
        await self._pool.close()

    async def ping(self) -> str | None:
        try:
            async with self._pool.connection(timeout=3) as conn:
                await conn.execute("SELECT 1")
            return None
        except Exception as e:
            return f"{type(e).__name__}: {str(e).strip()[:200]}"

    async def _migrate(self):
        files = sorted(
            (f for f in resources.files("app.migrations").iterdir() if f.name.endswith(".sql")),
            key=lambda f: f.name,
        )
        async with self._pool.connection() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                " version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            # Serialise migrations if two processes ever start together.
            await conn.execute("SELECT pg_advisory_lock(7461)")
            try:
                cur = await conn.execute("SELECT version FROM schema_version")
                done = {r[0] for r in await cur.fetchall()}
                for f in files:
                    version = int(f.name.split("_", 1)[0])
                    if version in done:
                        continue
                    async with conn.transaction():
                        await conn.execute(f.read_text())
                        await conn.execute(
                            "INSERT INTO schema_version (version) VALUES (%s)", (version,)
                        )
            finally:
                await conn.execute("SELECT pg_advisory_unlock(7461)")

    async def get_setting(self, key: str):
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = await cur.fetchone()
            return None if row is None else row[0]

    async def set_setting(self, key: str, value) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s::jsonb)"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (key, json.dumps(value)),
            )

    async def mark_notified(self, key: str) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO notifications_sent (key) VALUES (%s) ON CONFLICT DO NOTHING", (key,)
            )
            return cur.rowcount == 1

    async def upsert_iv(self, symbol: str, day: date, iv30: float, source: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO iv_history (symbol, day, iv30, source, recorded_at)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (symbol, day) DO UPDATE SET iv30 = EXCLUDED.iv30,"
                " source = EXCLUDED.source, recorded_at = EXCLUDED.recorded_at",
                (symbol, day, iv30, source, datetime.now(UTC)),
            )

    async def iv_history(self, symbol: str, since: date) -> list[tuple[date, float]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT day, iv30 FROM iv_history WHERE symbol = %s AND day >= %s ORDER BY day",
                (symbol, since),
            )
            return [(r[0], r[1]) for r in await cur.fetchall()]

    async def upsert_beta(self, symbol: str, beta: float, as_of: date) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO betas (symbol, beta, as_of) VALUES (%s, %s, %s)"
                " ON CONFLICT (symbol) DO UPDATE SET beta = EXCLUDED.beta, as_of = EXCLUDED.as_of",
                (symbol, beta, as_of),
            )

    async def upsert_transactions(self, items: list[dict], account: str = "live") -> int:
        from .chains import _ts

        new = 0
        async with self._pool.connection() as conn:
            for t in items:
                when = datetime.fromtimestamp(_ts(t.get("time") or t.get("tradeDate")), UTC)
                cur = await conn.execute(
                    "INSERT INTO schwab_transactions (account, activity_id, time, raw)"
                    " VALUES (%s, %s, %s, %s::jsonb)"
                    " ON CONFLICT (account, activity_id) DO UPDATE SET raw = EXCLUDED.raw"
                    " RETURNING (xmax = 0)",
                    (account, str(t.get("activityId")), when, json.dumps(t)),
                )
                row = await cur.fetchone()
                new += bool(row and row[0])
        return new

    async def upsert_sheet_rows(self, rows) -> dict:
        out = {"new": 0, "updated": 0, "unchanged": 0}
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT row_key, data FROM sheet_rows")
            existing = {k: d for k, d in await cur.fetchall()}
            async with conn.transaction():
                for r in rows:
                    old = existing.get(r.key)
                    if old == r.data:
                        out["unchanged"] += 1
                        continue
                    out["new" if old is None else "updated"] += 1
                    await conn.execute(
                        "INSERT INTO sheet_rows (row_key, account, data) VALUES (%s, %s, %s::jsonb)"
                        " ON CONFLICT (row_key) DO UPDATE SET data = EXCLUDED.data,"
                        " account = EXCLUDED.account, imported_at = now()",
                        (r.key, r.account, json.dumps(r.data)),
                    )
        return out

    async def sheet_rows(self, account: str) -> list[tuple[str, dict]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT row_key, data FROM sheet_rows WHERE account = %s", (account,)
            )
            return [(k, d) for k, d in await cur.fetchall()]

    async def sheet_accounts(self) -> dict[str, int]:
        """The history sheet's Account labels and how many rows each has."""
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT account, count(*) FROM sheet_rows GROUP BY account")
            return {a: int(n) for a, n in await cur.fetchall()}

    async def upsert_prices(self, symbol: str, closes: list[tuple[date, float]]) -> None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO daily_prices (symbol, day, close) VALUES (%s, %s, %s)"
                    " ON CONFLICT (symbol, day) DO UPDATE SET close = EXCLUDED.close",
                    [(symbol, d, c) for d, c in closes],
                )

    async def prices(self, symbols) -> dict[str, dict[date, float]]:
        symbols = list(symbols)
        out: dict[str, dict[date, float]] = {s: {} for s in symbols}
        if not symbols:
            return out
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT symbol, day, close FROM daily_prices WHERE symbol = ANY(%s)", (symbols,)
            )
            for s, d, c in await cur.fetchall():
                out[s][d] = c
        return out

    async def transactions(self, account: str = "live") -> list[dict]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT raw FROM schwab_transactions WHERE account = %s ORDER BY time", (account,)
            )
            return [r[0] for r in await cur.fetchall()]

    async def get_beta(self, symbol: str) -> tuple[float, date] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT beta, as_of FROM betas WHERE symbol = %s", (symbol,))
            row = await cur.fetchone()
            return None if row is None else (row[0], row[1])
