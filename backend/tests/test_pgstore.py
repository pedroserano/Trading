"""Runs against a real Postgres when TEST_DB_HOST is set (CI starts one)."""

import os
from datetime import date

import pytest

from app.store import PgStore

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DB_HOST"), reason="no test database")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_pgstore_roundtrip():
    store = PgStore(
        os.environ["TEST_DB_HOST"],
        int(os.environ.get("TEST_DB_PORT", "5432")),
        "trading",
        "trading",
        os.environ.get("TEST_DB_PASSWORD", "trading"),
    )
    await store.open()
    try:
        await store.open()  # migrations are idempotent
        assert await store.ping() is None
        await store.set_setting("account", {"number": "1", "last4": "1"})
        await store.set_setting("account", {"number": "2", "last4": "2"})
        assert await store.get_setting("account") == {"number": "2", "last4": "2"}
        assert await store.get_setting("missing") is None
        key = f"test:{os.getpid()}"
        assert await store.mark_notified(key) is True
        assert await store.mark_notified(key) is False
        await store.upsert_iv("TST", date(2026, 1, 2), 15.0, "t")
        await store.upsert_iv("TST", date(2026, 1, 2), 16.0, "t")
        await store.upsert_iv("TST", date(2025, 1, 2), 30.0, "t")
        assert await store.iv_history("TST", date(2026, 1, 1)) == [(date(2026, 1, 2), 16.0)]
        await store.upsert_beta("TST", 1.2, date(2026, 1, 2))
        assert await store.get_beta("TST") == (1.2, date(2026, 1, 2))
    finally:
        await store.close()
