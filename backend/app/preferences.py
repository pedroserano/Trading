"""Settings changed from the app's Settings page, kept in the database: watchlist and rules."""

import csv
import io
import re
from dataclasses import asdict, fields

from .strategies import Rules
from .targets import BIAS_BANDS, BIASES

MAX_WATCHLIST = 200
# Stocks, ETFs and indexes as Schwab spells them: SPY, BRK.B, BF/B, $SPX.
_SYMBOL = re.compile(r"^\$?[A-Z][A-Z0-9]{0,5}([./][A-Z])?$")

# Editable rules: (field, label, minimum, maximum, step). Fractions are shown as percent.
RULE_FIELDS = [
    ("profit_target", "Profit target", 0.05, 1.0, "percent"),
    ("profit_target_straddle", "Profit target, straddles and iron flies", 0.05, 1.0, "percent"),
    (
        "profit_target_ratio",
        "Profit target, front ratios (1×2), of max profit",
        0.05,
        1.0,
        "percent",
    ),
    (
        "profit_target_calendar",
        "Profit target, calendars and diagonals, of the debit",
        0.02,
        1.0,
        "percent",
    ),
    ("manage_dte", "Manage at DTE", 0, 60, "int"),
    ("tested_delta", "Tested at short delta", 0.05, 0.95, "delta"),
    ("loss_multiple", "Loss flag at multiple of credit", 0.5, 10.0, "multiple"),
    ("max_underlying_bp", "Buying power in one underlying, at most", 0.01, 1.0, "percent"),
    ("max_index_bp", "Buying power in one index underlying, at most", 0.01, 1.0, "percent"),
]
# Portfolio targets, on the Settings page under Portfolio targets. Same shape as RULE_FIELDS.
BAND_FIELDS = [
    ("delta_min", "Beta-weighted delta, at least (share of net liq)", -1.0, 1.0, "percent"),
    ("delta_max", "Beta-weighted delta, at most (share of net liq)", -1.0, 1.0, "percent"),
    ("theta_min", "Daily theta, at least (share of net liq a day)", 0.0, 0.02, "percent"),
    ("theta_max", "Daily theta, at most (share of net liq a day)", 0.0, 0.02, "percent"),
]
# Portfolio margin asks roughly half Reg T's margin for short premium, so the same share of
# net liq carries about twice the exposure. tastytrade's guidance is to size smaller under
# portfolio margin; these defaults halve every buying-power limit to keep the exposure alike.
MARGIN_TYPES = {"reg_t": "Reg T margin", "pm": "Portfolio margin"}
MARGIN_SCALE = {"reg_t": 1.0, "pm": 0.5}
MARGIN_DEFAULTS = {
    "reg_t": {"max_underlying_bp": 0.15, "max_index_bp": 0.50},
    "pm": {"max_underlying_bp": 0.075, "max_index_bp": 0.25},
}

# Shown by the fields that refuse orders, so their effect is plain.
RULE_NOTES = {
    "profit_target_ratio": "A front ratio makes the most at its short strike at expiration: the "
    "width plus the credit. That is only realistic in the last days, so the target is a small "
    "share of it: at least 10%, per Tony Battista (tastylive).",
    "profit_target_calendar": "A share of the debit paid. tastylive's calendar studies found "
    "10% the best exit for success rate and P/L per day; they aim for 10% to 25%.",
    "max_underlying_bp": "Hard limit: an opening order that would take one underlying's buying "
    "power above this share of net liq is refused, for you, automanage and the scanner.",
    "max_index_bp": "The same limit for SPY, QQQ, IWM, DIA and the SPX, XSP, NDX and RUT indexes.",
    "margin_type": "Choosing one saves at once and resets the buying-power limits to the "
    "margin type's defaults: under portfolio margin each one is half the Reg T figure (one "
    "underlying 7.5%, an index 25%, one scanner or live trade 2.5%, the VIX ceiling and the "
    "ticket's size guides halved), because the same buying power carries about twice the "
    "exposure. You can adjust them after.",
    "delta_bias": "Choosing a bias saves at once and sets the delta band to its default: "
    "neutral -5% to +5% of net liq, bullish 0 to +15%, bearish -15% to 0. You can adjust the "
    "band after. Neutral is tastylive's default: new trades are symmetric and a tested trade "
    "is rolled back to neutral. Bullish: the scanner sells puts closer than calls until "
    "portfolio delta reaches the middle of the band, and a tested-side roll leaves the trade "
    "slightly long rather than flat. Bearish is the mirror. Profit targets, 21 DTE and the "
    "other exits are the same whatever the bias.",
    "block_over_ceiling": "Hard limit: an opening order that would take total buying power in "
    "use above the ceiling for the current VIX is refused. Unticked, it is only a warning.",
}


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
        # The bias as stored before this update: a new bias resets the band below, after the
        # band fields (sent with the old band by the same form) have been read.
        old_bias = current.get("delta_bias", "neutral")
        for name, label, lo, hi, kind in RULE_FIELDS + BAND_FIELDS:
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
        if form.get("_form") != "targets":  # checkboxes are on the rules form only
            current["notify_flags"] = form.get("notify_flags") in ("on", "true", "1", True)
            current["block_over_ceiling"] = form.get("block_over_ceiling") in (
                "on",
                "true",
                "1",
                True,
            )
        new_type = form.get("margin_type", current.get("margin_type", "reg_t"))
        if new_type not in MARGIN_TYPES:
            raise ValueError("Margin type: Reg T or portfolio margin")
        if new_type != current.get("margin_type", "reg_t"):
            current.update(MARGIN_DEFAULTS[new_type])
        current["margin_type"] = new_type
        bias = form.get("delta_bias", old_bias)
        if bias not in BIASES:
            raise ValueError("Delta bias: neutral, bullish or bearish")
        if bias != old_bias:
            current["delta_min"], current["delta_max"] = BIAS_BANDS[bias]
        current["delta_bias"] = bias
        if current["delta_min"] > current["delta_max"]:
            raise ValueError("Beta-weighted delta: the least must not be above the most")
        if current["theta_min"] > current["theta_max"]:
            raise ValueError("Daily theta: the least must not be above the most")
        await self.store.set_setting("rules", current)
        return Rules(**current)
