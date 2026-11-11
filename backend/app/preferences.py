"""Settings changed from the app's Settings page, kept in the database: watchlist and rules."""

import csv
import io
import re
from dataclasses import asdict, fields

from .strategies import Rules

MAX_WATCHLIST = 200
# Stocks, ETFs and indexes as Schwab spells them: SPY, BRK.B, BF/B, $SPX.
_SYMBOL = re.compile(r"^\$?[A-Z][A-Z0-9]{0,5}([./][A-Z])?$")

# Editable rules: (field, label, minimum, maximum, step). Fractions are shown as percent.
RULE_FIELDS = [
    ("profit_target", "Profit target", 0.05, 1.0, "percent"),
    ("profit_target_straddle", "Profit target, straddles and iron flies", 0.05, 1.0, "percent"),
    ("manage_dte", "Manage at DTE", 0, 60, "int"),
    ("tested_delta", "Tested at short delta", 0.05, 0.95, "delta"),
    ("loss_multiple", "Loss flag at multiple of credit", 0.5, 10.0, "multiple"),
]


def parse_symbols(text: str) -> tuple[list[str], list[str]]:
    """Symbols from pasted text or a thinkorswim watchlist export.

    Returns (symbols in order, skipped entries). thinkorswim exports a CSV with a few
    lines of title text, then a header row with a "Symbol" column. Pasted lists can be
    separated by commas, spaces or new lines.
    """
    rows = list(csv.reader(io.StringIO(text.replace("\r", ""))))
    column = None
    start = 0
    for i, row in enumerate(rows):
        cells = [c.strip().lower() for c in row]
        if "symbol" in cells:
            column, start = cells.index("symbol"), i + 1
            break
    raw: list[str] = []
    if column is not None:
        raw = [r[column] for r in rows[start:] if len(r) > column]
    else:
        raw = re.split(r"[\s,;]+", text)
    symbols, skipped, seen = [], [], set()
    for entry in raw:
        s = entry.strip().strip('"').upper()
        if not s:
            continue
        if not _SYMBOL.match(s):
            # Futures (/ES), options (.SPY...), and anything else the app cannot trade.
            skipped.append(s)
            continue
        if s not in seen:
            seen.add(s)
            symbols.append(s)
    return symbols, skipped


class Preferences:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings

    async def watchlist(self) -> list[str]:
        saved = await self.store.get_setting("watchlist")
        return list(saved) if saved is not None else self.settings.watchlist_symbols

    async def set_watchlist(self, symbols: list[str]) -> None:
        await self.store.set_setting("watchlist", symbols[:MAX_WATCHLIST])

    async def rules(self) -> Rules:
        saved = await self.store.get_setting("rules") or {}
        known = {f.name for f in fields(Rules)}
        return Rules(**{k: v for k, v in saved.items() if k in known})

    async def set_rules(self, form: dict) -> Rules:
        """Validates form values (strings from the Settings page) and saves them."""
        current = asdict(await self.rules())
        for name, label, lo, hi, kind in RULE_FIELDS:
            if name not in form:
                continue
            try:
                v = float(form[name])
            except ValueError as e:
                raise ValueError(f"{label}: not a number") from e
            if kind == "percent":
                v = v / 100
            if not lo <= v <= hi:
                shown = (lo * 100, hi * 100) if kind == "percent" else (lo, hi)
                raise ValueError(f"{label}: must be between {shown[0]:g} and {shown[1]:g}")
            current[name] = int(v) if kind == "int" else v
        current["notify_flags"] = form.get("notify_flags") in ("on", "true", "1", True)
        await self.store.set_setting("rules", current)
        return Rules(**current)
