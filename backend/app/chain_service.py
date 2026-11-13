"""Keeps the Schwab account's trade chains current.

Pulls new transactions every few minutes (and soon after account activity), stores them, and
rebuilds the chains against the positions Schwab reports.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from . import chains, sheet_import
from .acctkeys import key_for
from .schwab_service import SchwabError, SchwabNotConnected

log = logging.getLogger(__name__)
SYNC_INTERVAL_S = 300
BACKFILL_DAYS = 365
CHUNK_DAYS = 60
OVERRIDES_KEY = "chain_overrides"
REVIEW_KEY = "sheet_review"
SHEET_ACCOUNT = "Joint"
CURSOR_KEY = "transactions_synced_to"


class ChainService:
    def __init__(self, source, store, portfolio, resolve_account, account="live", sheet_label=None):
        self.source = source
        self.store = store
        self.portfolio = portfolio
        self.resolve_account = resolve_account
        # Which Schwab account this is, and which of the sheet's Account labels is its history
        # ("Joint" for the account the app first used).
        self.account = account
        self.sheet_label = sheet_label or (lambda: SHEET_ACCOUNT if account == "live" else None)
        self.result: dict = {"chains": [], "proposals": [], "unmatched": [], "mismatches": []}
        self.synced_at: float | None = None
        self.error: str | None = None
        self._wake = asyncio.Event()
        self._by_id: dict[str, dict] = {}
        # The imported spreadsheet: conversion problems, review list, history-only records.
        self.history: dict = {
            "rows": 0,
            "problems": [],
            "review": [],
            "matched": 0,
            "futures": [],
            "realized": [],
            "window_start": None,
        }

    def key(self, base: str) -> str:
        return key_for(base, self.account)

    def refresh_soon(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except (SchwabNotConnected, SchwabError) as e:
                self.error = str(e)
            except Exception as e:
                log.exception("Trade chain sync failed")
                self.error = f"{type(e).__name__}: {e}"
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=SYNC_INTERVAL_S)
                await asyncio.sleep(20)  # let Schwab record the transaction first
            except TimeoutError:
                pass

    async def sync(self) -> int:
        account_hash = await self.resolve_account()
        now = datetime.now(UTC)
        cursor = await self.store.get_setting(self.key(CURSOR_KEY))
        if cursor:
            start = datetime.fromtimestamp(cursor, UTC) - timedelta(days=2)
        else:
            start = now - timedelta(days=BACKFILL_DAYS)
        new = 0
        while start < now:
            end = min(start + timedelta(days=CHUNK_DAYS), now)
            items = await self.source.transactions(account_hash, start, end)
            new += await self.store.upsert_transactions(list(items or []), self.account)
            start = end
        await self.store.set_setting(self.key(CURSOR_KEY), now.timestamp())
        if cursor is None:
            await self.store.set_setting(
                self.key("transactions_history_start"),
                (now - timedelta(days=BACKFILL_DAYS)).timestamp(),
            )
        await self.rebuild()
        self.synced_at = time.time()
        self.error = None
        if new:
            log.info("Stored %d new Schwab transactions", new)
            self.portfolio.refresh_soon()
        return new

    async def rebuild(self) -> None:
        raws = await self.store.transactions(self.account)
        groups = chains.merge([g for r in raws if (g := chains.normalize(r)) is not None])
        positions = {}
        acct = (self.portfolio.account_raw or {}).get("securitiesAccount", {})
        for p in acct.get("positions", []):
            inst = p.get("instrument", {})
            qty = float(p.get("longQuantity", 0) or 0) - float(p.get("shortQuantity", 0) or 0)
            if not qty or inst.get("assetType") not in chains.LEG_TYPES:
                continue
            positions[inst["symbol"]] = {
                "quantity": qty,
                "avg_price": _avg(p, qty),
                "underlying": inst.get("underlyingSymbol") or inst["symbol"],
                "multiplier": 100 if inst.get("assetType") == "OPTION" else 1,
            }
        overrides = await self.store.get_setting(self.key(OVERRIDES_KEY)) or {}
        start = await self.store.get_setting(self.key("transactions_history_start"))

        # The spreadsheet's history: used as is before Schwab's record starts, reconciled
        # against Schwab's fills after.
        schwab_only, schwab_start = list(groups), start
        label = self.sheet_label()
        rows = await self.store.sheet_rows(label) if label else []
        if rows:
            try:
                conv = sheet_import.convert(
                    [sheet_import.SheetRow(key=k, account=label, data=d) for k, d in rows]
                )
                window = sheet_import.window_start_from(groups, start) if groups else None
                decisions = await self.store.get_setting(self.key(REVIEW_KEY)) or {}
                rec = sheet_import.reconcile(conv.groups, groups, window or float("inf"), decisions)
                first_sheet = min((g.time for g in conv.groups), default=None)
                self.history = {
                    "rows": len(rows),
                    "problems": conv.problems,
                    "review": rec["review"],
                    "matched": rec["matched"],
                    "futures": conv.futures,
                    "realized": conv.realized,
                    "window_start": window,
                }
                groups = sorted(groups + rec["use"], key=lambda g: g.time)
                if first_sheet is not None:
                    start = first_sheet - 86400
            except Exception as e:
                # Keep Schwab's history usable and say what went wrong with the sheet's.
                log.exception("Merging the imported spreadsheet failed")
                self.history = {
                    **self.history,
                    "rows": len(rows),
                    "error": f"Merging the imported spreadsheet failed: {type(e).__name__}: {e}",
                }
            else:
                self.history["error"] = None
        else:
            self.history = {**self.history, "rows": 0, "error": None}
        try:
            self.result = chains.build(groups, positions, overrides, start)
        except Exception as e:
            if len(groups) == len(schwab_only):
                raise
            log.exception("Building trades with the imported spreadsheet failed")
            self.history["error"] = (
                f"Building trades from the imported spreadsheet failed: {type(e).__name__}: {e}"
            )
            self.result = chains.build(schwab_only, positions, overrides, schwab_start)
        self._by_id = {c["id"]: c for c in self.result["chains"]}

    # For the portfolio ---------------------------------------------------------------

    def trade_info(self) -> dict:
        return chains.trade_info(self.result["chains"])

    def holdings(self) -> dict[str, dict[str, float]]:
        return {c["id"]: c["holdings"] for c in self.result["chains"] if c["status"] == "open"}

    # Manual corrections --------------------------------------------------------------

    async def link(self, group_key: str, chain_id: str) -> None:
        if chain_id not in self._by_id:
            raise ValueError("No such chain")
        o = await self.store.get_setting(self.key(OVERRIDES_KEY)) or {}
        o.setdefault("link", {})[group_key] = chain_id
        o["split"] = [g for g in o.get("split", []) if g != group_key]
        await self.store.set_setting(self.key(OVERRIDES_KEY), o)
        await self.rebuild()

    async def split(self, group_key: str) -> None:
        o = await self.store.get_setting(self.key(OVERRIDES_KEY)) or {}
        o.setdefault("link", {}).pop(group_key, None)
        if group_key not in o.setdefault("split", []):
            o["split"].append(group_key)
        await self.store.set_setting(self.key(OVERRIDES_KEY), o)
        await self.rebuild()


def _avg(p: dict, qty: float) -> float:
    side = p.get("averageShortPrice") if qty < 0 else p.get("averageLongPrice")
    for v in (side, p.get("averagePrice")):
        try:
            if v is not None:
                return abs(float(v))
        except (TypeError, ValueError):
            continue
    return 0.0
