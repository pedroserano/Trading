"""Scanner: finds new trades the tastytrade way and hands them to automanage's levels.

Once a day (and on Scan now) it looks at each candidate underlying and opens premium only
when IV rank is high enough: a 16-delta strangle when IV rank is high, a 16-delta iron
condor collecting at least a third of its width otherwise, in the monthly expiration nearest
45 DTE. It skips thin markets, single stocks without a known earnings date (the earnings
buffer), underlyings already traded, and trades that would take too much buying power.

What it finds goes through the same levels as automanage: Notify, Propose (waits for a tap)
or Auto (simulated account only; live stops at Propose). Every order uses the same ticket,
guards and preview as a manual one.
"""

import asyncio
import copy
import logging
import math
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import cash as cash_mod
from . import chain as chain_mod
from . import dividends, plain, portfolio_view, targets
from .automation import LEVELS, Action, _hm
from .extremes import compute as compute_extreme
from .options import parse_option_symbol
from .signals import drop_signal, one_side, shock, skew

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
SETTINGS_KEY = "scanner_settings"
STATE_KEY = "scanner"
DEFAULTS = {
    "level_sim": "propose",
    "level_live": "off",
    "level_by_account": {},  # every other Schwab account's level, by account key
    "min_ivr": 30,
    "undefined_ivr": 50,
    "target_dte": 45,
    "min_dte": 30,
    "max_dte": 60,
    "monthly_only": True,
    "short_delta": 0.16,
    "ic_short_delta": 0.16,
    "min_ic_credit": 1 / 3,
    "earnings_days": 7,
    "max_spread": 0.05,
    "min_oi": 1000,
    "max_bp_share": 0.05,
    "max_per_underlying": 1,
    "max_new_per_day": 3,
    "contracts": 1,
    "scan_time": "10:15",
    "automanage_new": True,
    # Price extremes: lean the trade against an overextended move.
    "extreme_tilt": True,
    "range_band": 0.05,
    "move_days": 5,
    "move_sigma": 2.0,
    "day_ratio": 2.0,
    "tilt_delta": 0.25,
    # Portfolio targets: outside the delta band, lean new trades back toward it.
    "band_lean": True,
    # A single stock's trade never holds through its next report.
    "avoid_through_earnings": True,
    "min_pop": 0.55,  # probability of profit at entry, at least
    # Absolute IV, alongside IV rank (which alone selects and ranks): a floor for quiet
    # markets, premium rich enough to sell at any IV rank (iron condors only), and implied
    # over realized volatility. 0 turns each off.
    "min_iv30": 15.0,
    "rich_iv30": 40.0,
    "min_iv_hv": 1.0,
    # Extreme volatility: defined risk only (margin balloons, and volatility can keep
    # expanding before it contracts). 0 turns it off.
    "defined_vix": 30.0,
    # Spread across sectors, so one wrong read cannot sink the account. 0: no limit.
    "max_per_sector": 2,
    # Selling into a sharp drop: a drop, IV rank at least drop_ivr and put skew at least
    # steep_skew (vol points) is the drop signal, considered first.
    "drop_ivr": 60,
    "steep_skew": 5.0,
    # A possible unscheduled shock (not an earnings report): a day's move this many times
    # its average, or IV30 up this share in 5 sessions, within shock_sessions. Then defined
    # risk and half size.
    "shock_day_ratio": 3.0,
    "shock_iv_jump": 0.30,
    "shock_sessions": 10,
    # A single stock whose report falls inside the trade: "skip" it, or "defined" risk at
    # half size.
    "earnings_inside": "skip",
    "max_bp_day": 0.10,  # buying power the scanner may add in one day, share of net liq
    "scan_every": 0,  # minutes between scans after the scan time (0: once a day)
}
# (key, label, low, high, kind, unit) for the Settings page.
FIELDS = [
    ("min_ivr", "Open only if IV rank is at least", 0, 100, "int", "IVR"),
    ("undefined_ivr", "Strangle (undefined risk) if IV rank is at least", 0, 100, "int", "IVR"),
    ("target_dte", "Expiration nearest", 7, 120, "int", "DTE"),
    ("min_dte", "No sooner than", 1, 120, "int", "DTE"),
    ("max_dte", "No later than", 7, 180, "int", "DTE"),
    ("short_delta", "Strangle short strikes at", 0.05, 0.5, "delta", "Δ"),
    ("ic_short_delta", "Iron condor short strikes at", 0.05, 0.5, "delta", "Δ"),
    ("min_ic_credit", "Iron condor credit, at least", 10, 60, "percent", "% of width"),
    (
        "earnings_days",
        "Single stocks: open only if earnings are more than",
        0,
        60,
        "int",
        "sessions away",
    ),
    ("max_spread", "At-the-money bid/ask spread, at most", 1, 50, "percent", "% of mark"),
    ("min_oi", "At-the-money open interest, at least", 0, 1_000_000, "int", "contracts"),
    ("max_bp_share", "Buying power for one trade, at most", 0.5, 50, "percent", "% net liq"),
    ("max_per_underlying", "Open trades per underlying, at most", 1, 10, "int", "trades"),
    ("max_new_per_day", "New trades per day, at most", 0, 50, "int", "trades"),
    ("max_per_sector", "Open trades per sector, at most (0: no limit)", 0, 50, "int", "trades"),
    ("defined_vix", "Iron condors only while VIX is at least (0: off)", 0, 100, "float", "VIX"),
    ("drop_ivr", "Drop signal: IV rank at least", 0, 100, "int", "IVR"),
    ("steep_skew", "Drop signal: put skew at least", 0, 30, "float", "vol pts"),
    ("shock_day_ratio", "Possible shock: a day's move at least", 1, 20, "float", "× average"),
    ("shock_iv_jump", "Possible shock: or IV30 up in 5 sessions at least", 5, 300, "percent", "%"),
    ("shock_sessions", "Possible shock: look back and stay careful for", 1, 60, "int", "sessions"),
    ("contracts", "Contracts per trade", 1, 50, "int", "contracts"),
    ("min_pop", "Probability of profit, at least", 0, 95, "percent", "%"),
    ("min_iv30", "IV30 at least (quiet-market floor; 0: off)", 0, 100, "float", "%"),
    ("rich_iv30", "Or IV30 at least, at any IV rank (0: off)", 0, 200, "float", "%"),
    ("min_iv_hv", "IV30 over 30-day realized volatility, at least (0: off)", 0, 3, "float", "×"),
    ("max_bp_day", "Buying power added in one day, at most", 0.5, 100, "percent", "% net liq"),
    ("scan_every", "Scan again every (0: once a day)", 0, 240, "int", "min"),
    ("range_band", "Price extreme: near a 52-week high or low, within", 0.5, 25, "percent", "%"),
    ("move_days", "Price extreme: move over", 2, 30, "int", "sessions"),
    ("move_sigma", "Price extreme: move vs expected, at least", 0.5, 5, "float", "σ"),
    ("day_ratio", "Price extreme: today's move, at least", 1, 10, "float", "× average"),
    ("tilt_delta", "At an extreme, the favoured side's short strike at", 0.1, 0.5, "delta", "Δ"),
]
FUNDS = {"ETF", "ETN", "COLLECTIVE_INVESTMENT", "INDEX", "MUTUAL_FUND"}


def account_level(s: dict, account: str) -> str:
    """The scanner's level for an account: the simulated account's, the first Schwab
    account's (level_live), or another Schwab account's (level_by_account, Off unset)."""
    if account in ("sim", "live"):
        return s[f"level_{account}"]
    return s.get("level_by_account", {}).get(account, "off")


def merged(saved: dict | None) -> dict:
    s = copy.deepcopy(DEFAULTS)
    s.update({k: v for k, v in (saved or {}).items() if k in DEFAULTS})
    return s


def parse_form(form: dict, current: dict | None) -> dict:
    """Validates the Settings form. Raises ValueError with a message for the page."""
    out = merged(current)
    for acct in ("sim", "live"):
        v = form.get(f"level_{acct}")
        if v is not None:
            if v not in LEVELS:
                raise ValueError("Scanner: unknown level")
            out[f"level_{acct}"] = v
    for key, label, lo, hi, kind, _unit in FIELDS:
        if key not in form:
            continue
        try:
            v = float(form[key])
        except ValueError:
            raise ValueError(f"{label}: not a number") from None
        if not lo <= v <= hi:
            raise ValueError(f"{label}: must be between {lo:g} and {hi:g}")
        out[key] = v / 100 if kind == "percent" else int(v) if kind == "int" else v
    if form.get("_form") == "scanner":
        out["monthly_only"] = form.get("monthly_only") in ("on", "true", "1")
        out["automanage_new"] = form.get("automanage_new") in ("on", "true", "1")
        out["extreme_tilt"] = form.get("extreme_tilt") in ("on", "true", "1")
        out["band_lean"] = form.get("band_lean") in ("on", "true", "1")
        out["avoid_through_earnings"] = form.get("avoid_through_earnings") in ("on", "true", "1")
    if form.get("earnings_inside") in ("skip", "defined"):
        out["earnings_inside"] = form["earnings_inside"]
    if "scan_time" in form:
        try:
            h, m = _hm(form["scan_time"])
            assert (9, 30) <= (h, m) < (16, 0)
        except (ValueError, AssertionError):
            raise ValueError("Scan time: between 09:30 and 16:00") from None
        out["scan_time"] = f"{h:02d}:{m:02d}"
    if 0 < out["scan_every"] < 5:
        raise ValueError("Scan again every: 0 (once a day) or at least 5 minutes")
    if out["min_dte"] > out["target_dte"] or out["target_dte"] > out["max_dte"]:
        raise ValueError("Expiration: the DTE range must hold the target")
    return out


LEVEL_LABELS = {"off": "Off", "notify": "Notify", "propose": "Propose", "auto": "Auto"}
EARNINGS_INSIDE = [("skip", "skip the stock"), ("defined", "allow only a defined-risk spread")]


def scan_fields(s: dict, live_auto: bool = True, defaults: dict | None = None) -> dict:
    """The scanner's settings as plain.Field, for the step list (values and defaults; some
    defaults follow the margin type, so the page passes its own)."""
    d = {**DEFAULTS, **(defaults or {})}
    out = {
        key: plain.Field(kind, s[key], d[key], lo, hi)
        for key, _label, lo, hi, kind, _unit in FIELDS
    }
    out["scan_time"] = plain.Field("time", s["scan_time"], d["scan_time"])
    levels = list(LEVEL_LABELS.items())
    out["level_sim"] = plain.Field("select", s["level_sim"], d["level_sim"], options=levels)
    out["level_live"] = plain.Field(
        "select",
        s["level_live"],
        d["level_live"],
        options=[x for x in levels if live_auto or x[0] != "auto"],
    )
    out["earnings_inside"] = plain.Field(
        "select", s.get("earnings_inside", "skip"), d["earnings_inside"], options=EARNINGS_INSIDE
    )
    for key in (
        "monthly_only",
        "automanage_new",
        "extreme_tilt",
        "band_lean",
        "avoid_through_earnings",
    ):
        out[key] = plain.Field("check", s[key], d[key])
    return out


BIAS_LINES = {
    "bullish": "Then your portfolio bias, bullish (⟦link:change it|r-bias⟧): until portfolio "
    "delta reaches the middle of its band, the put side is sold a little closer.",
    "bearish": "Then your portfolio bias, bearish (⟦link:change it|r-bias⟧): until portfolio "
    "delta reaches the middle of its band, the call side is sold a little closer.",
    "contrarian": "Then your portfolio bias, contrarian (⟦link:change it|r-bias⟧): no lean of "
    "its own; sharp moves decide, against the move.",
    "neutral": "Then your portfolio bias, neutral, tastylive's default "
    "(⟦link:change it|r-bias⟧): no lean of its own.",
}


def scan_steps(bias: str, live_symbols: list[str] | None) -> list[tuple[str, list[str]]]:
    """How the scanner picks trades, in plain words and in order, with tokens where each
    setting goes (see plain.py)."""
    live = "SPY and your watchlist" if live_symbols is None else ", ".join(live_symbols)
    return [
        (
            "1. Which stocks it looks at",
            [
                "Once a day at ⟦scan_time⟧ ET, then again every ⟦scan_every⟧ minutes until "
                "the close (0: once a day), and whenever you tap Scan now.",
                "Simulated account: SPY and your ⟦link:watchlist|watchlist⟧. Schwab account: "
                f"{live} (⟦link:Schwab limits|live⟧).",
                "It opens strangles, iron condors, and one-sided puts or calls (or their "
                "spreads). Ratios, jade lizards and the rest are yours to open by hand.",
            ],
        ),
        (
            "2. Is the premium worth selling?",
            [
                "Options must be expensive for that stock: IV rank of ⟦min_ivr⟧ or more, "
                "against its own past year. Stocks are never compared by raw IV30, because "
                "some are always jumpier than others.",
                "Or IV30 of ⟦rich_iv30⟧% or more at any IV rank (0: off): premium rich enough "
                "to sell anyway, but only as a defined-risk iron condor.",
                "IV30 must be at least ⟦min_iv30⟧% (0: off): in a calm market even a high IV "
                "rank can pay too little.",
                "IV30 must be at least ⟦min_iv_hv⟧× how much the stock has really moved over "
                "30 sessions (0: off): the options must price in more movement than the stock "
                "is making.",
                "Easy to trade: an at-the-money bid/ask spread of ⟦max_spread⟧% or less, and "
                "open interest of ⟦min_oi⟧ or more.",
            ],
        ),
        (
            "3. Is now a bad time?",
            [
                "Single stocks need a known earnings date more than ⟦earnings_days⟧ sessions "
                "away (⟦link:earnings dates|earnings⟧). ETFs have no earnings.",
                "⟦?avoid_through_earnings⟧ Check for a report inside the trade; if there is "
                "one, ⟦earnings_inside⟧ at half size.",
                "A possible shock, such as a CEO leaving or an investigation: a day's move "
                "⟦shock_day_ratio⟧× the usual, or implied volatility up ⟦shock_iv_jump⟧% in 5 "
                "sessions, with no earnings report behind it. Then only defined-risk spreads "
                "at half size, for ⟦shock_sessions⟧ sessions.",
                "Nothing opens while the portfolio's daily theta is already at its maximum "
                "(⟦link:theta band|portfolio⟧).",
            ],
        ),
        (
            "4. Which way it leans (the first that applies)",
            [
                "⟦?band_lean⟧ If the whole portfolio is outside its ⟦link:delta band|portfolio⟧, "
                "lean back toward it (and lean with your bias, below).",
                "Your view on the stock, from the ⟦link:Watchlist|watchlist⟧ page (bullish, "
                "bearish, neutral, would own).",
                "A sharp drop with IV rank ⟦drop_ivr⟧ or more and put skew of ⟦steep_skew⟧ "
                "points or more is the drop signal: puts are dearest, so it sells puts, and "
                "these trades go first.",
                "⟦?extreme_tilt⟧ Lean against other price extremes too: within ⟦range_band⟧% "
                "of a 52-week high or low, a ⟦move_days⟧-session move of ⟦move_sigma⟧σ or "
                "more, or a day's move ⟦day_ratio⟧× the average.",
                BIAS_LINES.get(bias, BIAS_LINES["neutral"]),
            ],
        ),
        (
            "5. What it sells",
            [
                "In the expiration nearest ⟦target_dte⟧ days, between ⟦min_dte⟧ and "
                "⟦max_dte⟧ days; ⟦?monthly_only⟧ monthly expirations only.",
                "With no lean: a strangle at ⟦short_delta⟧ delta when IV rank is "
                "⟦undefined_ivr⟧ or more, otherwise an iron condor at ⟦ic_short_delta⟧ delta "
                "collecting at least ⟦min_ic_credit⟧% of its width. Iron condors only while "
                "VIX is ⟦defined_vix⟧ or more (0: off).",
                "Leaning on your view or a sharp move: one side only, at a strike outside the "
                "expected move, so it works whether the stock rebounds, stalls or falls a bit "
                "further. A short put (or call), or a spread when defined risk is required.",
                "Would own: a cash-secured short put only, and only when your cash and sweep "
                "cover it after your other Would own puts. Never margin.",
                "Leaning for the portfolio or your bias: the favoured side sold closer, at "
                "⟦tilt_delta⟧ delta.",
                "Probability of profit at least ⟦min_pop⟧%.",
            ],
        ),
        (
            "6. How big",
            [
                "⟦contracts⟧ contracts a trade, at most ⟦max_bp_share⟧% of net liq in buying "
                "power each, and at most ⟦max_per_underlying⟧ open trades in one stock.",
                "One stock at most ⟦rules:max_underlying_bp⟧ of net liq in buying power, and "
                "its uncovered short options at most ⟦rules:max_notional⟧ in what you could be "
                "made to buy, counting its share of your SPY and QQQ (look-through); over that, "
                "a spread instead.",
                "At most ⟦max_per_sector⟧ open trades per sector (0: no limit), so one wrong "
                "read cannot sink the account.",
                "At most ⟦max_new_per_day⟧ new trades a day, adding at most ⟦max_bp_day⟧% of "
                "net liq in buying power, and never past the buying-power ceiling for today's "
                "VIX (⟦link:the ceiling|rules⟧).",
            ],
        ),
        (
            "7. What happens next",
            [
                "Simulated account: ⟦level_sim⟧. Schwab account: ⟦level_live⟧. Notify sends a "
                "message, Propose waits for your tap, Auto places the order (the Schwab account "
                "only with LIVE_AUTO=true in .env).",
                "⟦?automanage_new⟧ Switch automanage on for each new trade once it fills, "
                "managed with the view it was opened with.",
                "Each run's trades, and what was skipped and why, show on the Watchlist page "
                "under Scanner.",
            ],
        ),
    ]


def rules_values(rules) -> dict[str, tuple[str, str]]:
    """Rules-tab values the step list shows: key -> (shown value, where to change it)."""
    if rules is None:
        return {}
    return {
        "max_underlying_bp": (
            f"{round(rules.max_underlying_bp * 100, 2):g}%",
            "r-max_underlying_bp",
        ),
        "max_notional": (f"{round(rules.max_notional * 100, 2):g}%", "r-max_notional"),
    }


def scan_map(s: dict, live_symbols: list[str] | None, bias: str = "neutral", rules=None) -> dict:
    """How the scanner picks trades, in plain words, from the current settings (for the API;
    the Settings page shows the same steps with the values editable in place)."""
    fields = scan_fields(s)
    rv = {k: v for k, (v, _) in rules_values(rules).items()}
    return {
        "steps": [
            (title, [plain.as_text(line, fields, rv) for line in lines])
            for title, lines in scan_steps(bias, live_symbols)
        ]
    }


def pick_expiration(exps: list[dict], s: dict) -> dict | None:
    ok = [e for e in exps if s["min_dte"] <= e["dte"] <= s["max_dte"]]
    if s["monthly_only"]:
        ok = [e for e in ok if e["monthly"]]
    return min(ok, key=lambda e: abs(e["dte"] - s["target_dte"])) if ok else None


def nearest_delta(rows: list[dict], side: str, target: float) -> dict | None:
    cands = [r for r in rows if r[side] and r[side]["delta"] is not None and r[side]["symbol"]]
    if not cands:
        return None
    return min(cands, key=lambda r: abs(abs(r[side]["delta"]) - target))


def _mid(c: dict | None) -> float | None:
    if not c:
        return None
    if c.get("mark") is not None:
        return c["mark"]
    if c.get("bid") is not None and c.get("ask") is not None:
        return (c["bid"] + c["ask"]) / 2
    return None


def _score(
    ivr: float,
    mid: float | None,
    n: int,
    bp: float | None,
    pop: float | None,
    iv_hv: float | None = None,
) -> float:
    """Ranking: IV rank (IV against its own history, never raw IV30, which would always
    favour naturally volatile names), the credit per dollar of buying power (premium size),
    the probability of profit, and a little for implied over realized volatility."""
    ret = (abs(mid or 0) * 100 * n / bp) if bp else 0.0
    edge = 0.5 * min(max((iv_hv or 1.0) - 1.0, 0.0), 1.0)
    return round(ivr / 100 + min(ret, 1.0) * 2 + (pop or 0) + edge, 4)


def _sector_name(key: str) -> str:
    return key.split(":")[0] if key.startswith(portfolio_view.UNKNOWN) else key


def realized_vol(closes: list, sessions: int = 21) -> float | None:
    """Annualised standard deviation of the last sessions' daily log returns, in percent,
    from (date, close) pairs; None with too little history."""
    values = [c for _, c in closes if c and c > 0][-(sessions + 1) :]
    if len(values) < sessions // 2 + 1:
        return None
    rets = [math.log(b / a) for a, b in zip(values, values[1:], strict=False)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
    return round(math.sqrt(var) * math.sqrt(252) * 100, 2)


def vol_check(ivr: float, iv30: float | None, hv: float | None, s: dict) -> tuple[str | None, bool]:
    """(reason to skip or None, admitted on rich IV30 alone). IV rank selects; absolute IV30
    and realized volatility only veto, or admit premium rich enough at any IV rank."""
    rich = bool(s.get("rich_iv30")) and iv30 is not None and iv30 >= s["rich_iv30"]
    if ivr < s["min_ivr"] and not rich:
        return f"IV rank {ivr:.0f}, under {s['min_ivr']}", False
    if s.get("min_iv30") and iv30 is not None and iv30 < s["min_iv30"]:
        return (
            f"IV30 {iv30:.1f}% is under {s['min_iv30']:g}%: IV rank {ivr:.0f} rests on a tiny "
            "range, so the premium is not worth the risk"
        ), False
    if s.get("min_iv_hv") and iv30 is not None and hv:
        if iv30 / hv < s["min_iv_hv"] - 1e-9:
            return (
                f"IV30 {iv30:.1f}% vs realized {hv:.1f}%: options price less movement than the "
                "stock is making"
            ), False
    return None, rich and ivr < s["min_ivr"]


def _theta_full(bands: dict) -> str | None:
    """Why no trade opens: the portfolio's theta is at or over the top of its band."""
    t = (bands or {}).get("theta") or {}
    if t.get("value") is None or t.get("max") is None or t["value"] < t["max"] - 1e-12:
        return None
    return (
        f"portfolio theta {t['value']:.2%} of net liq is at the {t['max']:.2%} maximum; "
        "no new trades until it is back inside the band"
    )


def _delta_push(preview: dict) -> str | None:
    """The warning when a trade would move beta-weighted delta further outside its band."""
    ba = preview.get("band_after") or {}
    if not ba.get("delta_push"):
        return None
    return next((w for w in ba.get("warnings", []) if w.startswith("Beta-weighted")), None)


def build_trade(
    rows: list[dict], strangle: bool, s: dict, tilt: str | None = None
) -> tuple[list[dict] | None, str]:
    """Legs (symbol, quantity per unit, effect) for one unit, or None and the reason.

    tilt "down" (the price has dropped too far) sells the put closer, at the tilt delta, for
    a bullish lean; "up" does the same with the call.
    """
    base = s["short_delta"] if strangle else s["ic_short_delta"]
    put_d = s["tilt_delta"] if tilt == "down" else base
    call_d = s["tilt_delta"] if tilt == "up" else base
    if strangle:
        put = nearest_delta(rows, "put", put_d)
        call = nearest_delta(rows, "call", call_d)
        if not put or not call or put["strike"] >= call["strike"]:
            return None, "no strikes near the target delta"
        return [
            {"symbol": put["put"]["symbol"], "quantity": -1, "effect": "open"},
            {"symbol": call["call"]["symbol"], "quantity": -1, "effect": "open"},
        ], f"Strangle {put['strike']:g}/{call['strike']:g}"
    sp = nearest_delta(rows, "put", put_d)
    sc = nearest_delta(rows, "call", call_d)
    if not sp or not sc or sp["strike"] >= sc["strike"]:
        return None, "no strikes near the target delta"
    by_strike = {r["strike"]: r for r in rows}
    puts = sorted(k for k, r in by_strike.items() if k < sp["strike"] and r["put"])
    calls = sorted(k for k, r in by_strike.items() if k > sc["strike"] and r["call"])
    best_ratio = 0.0
    # Narrowest wings first: the first width that collects enough is the iron condor.
    widths = sorted(
        {round(sp["strike"] - k, 4) for k in puts} & {round(k - sc["strike"], 4) for k in calls}
    )
    for w in widths:
        lp, lc = by_strike[sp["strike"] - w], by_strike[sc["strike"] + w]
        prices = [_mid(sp["put"]), _mid(lp["put"]), _mid(sc["call"]), _mid(lc["call"])]
        if any(p is None for p in prices):
            continue
        credit = prices[0] - prices[1] + prices[2] - prices[3]
        ratio = credit / w if w else 0
        best_ratio = max(best_ratio, ratio)
        if ratio >= s["min_ic_credit"]:
            return [
                {"symbol": lp["put"]["symbol"], "quantity": 1, "effect": "open"},
                {"symbol": sp["put"]["symbol"], "quantity": -1, "effect": "open"},
                {"symbol": sc["call"]["symbol"], "quantity": -1, "effect": "open"},
                {"symbol": lc["call"]["symbol"], "quantity": 1, "effect": "open"},
            ], f"Iron condor {lp['strike']:g}/{sp['strike']:g}/{sc['strike']:g}/{lc['strike']:g}"
    return None, (
        f"no wing width collects {s['min_ic_credit']:.0%} of the width "
        f"(best {best_ratio:.0%}); lower the minimum or move the short strikes closer"
    )


class Scanner:
    TICK_S = 60

    def __init__(
        self,
        store,
        source,
        portfolios,
        automations,
        watchlist,
        preferences,
        brokers,
        clock=lambda: datetime.now(ET),
        earnings=None,
        history=None,
    ):
        self.store = store
        self.source = source
        self.portfolios = portfolios
        self.automations = automations
        self.watchlist = watchlist
        self.preferences = preferences
        # Each Schwab account's orders and limits, by key (a single broker: the first account).
        self.brokers = brokers if isinstance(brokers, dict) else {"live": brokers}
        self.clock = clock
        self.earnings = earnings
        self.history = history
        self.lock = asyncio.Lock()

    async def settings(self) -> dict:
        return merged(await self.store.get_setting(SETTINGS_KEY))

    async def _state(self) -> dict:
        return await self.store.get_setting(STATE_KEY) or {}

    def level(self, s: dict, account: str) -> str:
        lv = account_level(s, account)
        cap = self.automations[account].max_level
        return LEVELS[min(LEVELS.index(lv), LEVELS.index(cap))]

    async def view(self, account: str) -> dict:
        s = await self.settings()
        st = await self._state()
        return {"settings": s, "level": self.level(s, account), "last": st.get(account)}

    async def symbols(self, account: str) -> list[str]:
        if account in self.brokers:
            c = await self.brokers[account].config()
            if not c.get("all_symbols"):
                return list(c["symbols"])
        out = ["SPY"] + [s for s in await self.preferences.watchlist() if s != "SPY"]
        return out

    async def _ivr(self, account: str, symbol: str) -> float | None:
        if symbol == "SPY":
            snap = self.portfolios["live"].snapshot or {}
            return ((snap.get("ivr") or {}).get("SPY") or {}).get("rank")
        row = next((r for r in await self.watchlist.rows() if r["symbol"] == symbol), None)
        return ((row or {}).get("ivr") or {}).get("rank")

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scanner tick failed")
            await asyncio.sleep(self.TICK_S)

    async def tick(self) -> list[str]:
        """Runs the daily scan for each account once its scan time has passed."""
        s = await self.settings()
        now = self.clock().astimezone(ET)
        today = now.date().isoformat()
        done = []
        if now.weekday() >= 5 or (now.hour, now.minute) < _hm(s["scan_time"]):
            return done
        st = await self._state()
        for account in list(self.automations):
            if self.level(s, account) == "off":
                continue
            last = st.get(account) or {}
            if last.get("day") == today:
                every = s.get("scan_every") or 0
                # Rescans during the day stop with automanage's window.
                if not every or time.time() - (last.get("at") or 0) < every * 60:
                    continue
                if (now.hour, now.minute) >= _hm(
                    (await self.automations[account].settings())["window_end"]
                ):
                    continue
            if (self.portfolios[account].snapshot or {}).get("session") != "open":
                continue
            await self.scan(account, act=True)
            done.append(account)
        return done

    async def scan(self, account: str, act: bool = True) -> dict:
        """Looks for trades now. With act, applies the account's scanner level to them."""
        async with self.lock:
            s = await self.settings()
            now = self.clock().astimezone(ET)
            today = now.date().isoformat()
            st = await self._state()
            prev = st.get(account) or {}
            placed_today = prev.get("new_today", 0) if prev.get("day") == today else 0
            bp_today = prev.get("bp_today", 0.0) if prev.get("day") == today else 0.0
            level = self.level(s, account)
            auto = self.automations[account]
            snap = self.portfolios[account].snapshot or {}
            acct = snap.get("account") or {}
            net_liq = acct.get("net_liq")
            ceiling = acct.get("bp_ceiling_pct")
            held = {
                u["symbol"]: sum(1 for x in u["strategies"] if x.get("trade_id") is not None)
                for u in snap.get("underlyings", [])
            }
            held_exps = {
                (u["symbol"], x["numbers"].get("expiration"))
                for u in snap.get("underlyings", [])
                for x in u["strategies"]
            }
            vix = (snap.get("market") or {}).get("vix") or (
                (self.portfolios["live"].snapshot or {}).get("market") or {}
            ).get("vix")
            defined_only = (
                bool(s.get("defined_vix")) and vix is not None and vix >= s["defined_vix"]
            )
            by_sector: dict[str, int] = {}
            for u in snap.get("underlyings", []):
                n_open = sum(1 for x in u["strategies"] if x.get("trade_id") is not None)
                if n_open:
                    key = self._sector(u["symbol"])
                    by_sector[key] = by_sector.get(key, 0) + n_open
            rules = await self.preferences.rules()
            bands = snap.get("bands") or {}
            full = _theta_full(bands)
            bias = bands.get("bias") or getattr(rules, "delta_bias", "neutral")
            # Outside the band: back toward it, first of all. Inside: a bullish or bearish
            # bias leans until the middle of the band.
            band_out = targets.lean({**bands, "bias": "neutral"}) if bands else None
            bias_in = None if band_out else targets.lean(bands)
            # Cash already promised to open Would own puts.
            own_reserved = sum(
                cash_mod.put_cost(x)
                for u in snap.get("underlyings", [])
                for x in u["strategies"]
                if x.get("trade_id") is not None
                and (auto.state or {}).get("trades", {}).get(str(x["trade_id"]), {}).get("view")
                == "would_own"
            )
            views_of = getattr(self.preferences, "symbol_views", None)
            symbol_views = await views_of() if views_of else {}
            candidates, skipped = [], []
            symbols = await self.symbols(account)
            try:
                quotes = await self.source.quotes(symbols)
            except Exception as e:
                quotes = {}
                log.warning("Scanner quotes failed: %s", e)
            for sym in symbols:
                if full:
                    skipped.append({"symbol": sym, "reason": full})
                    continue
                why = await self._check(account, sym, s, held, quotes)
                if isinstance(why, str):
                    skipped.append({"symbol": sym, "reason": why})
                    continue
                ivr, exp, rows, vol = why
                if (sym, exp["date"]) in held_exps:
                    skipped.append(
                        {"symbol": sym, "reason": f"already a trade here in {exp['date']}"}
                    )
                    continue
                sector = self._sector(sym)
                if s.get("max_per_sector") and by_sector.get(sector, 0) >= s["max_per_sector"]:
                    skipped.append(
                        {
                            "symbol": sym,
                            "reason": f"already {by_sector[sector]} open trades in "
                            f"{_sector_name(sector)} (at most {s['max_per_sector']} per sector)",
                        }
                    )
                    continue
                # IV rank decides the risk taken; premium admitted on IV30 alone, at a low IV
                # rank, gets defined risk only.
                # An earnings report inside the trade (when allowed) or a possible unscheduled
                # shock: defined risk and half size.
                careful = bool(vol["caution"])
                strangle = (
                    ivr >= s["undefined_ivr"]
                    and not vol["rich_only"]
                    and not defined_only
                    and not careful
                )
                ext = await self._extreme(sym, quotes, s)
                drop = drop_signal((ext or {}).get("direction"), ivr, vol["skew"], s)
                ext_dir = (
                    (ext or {}).get("direction")
                    if s["extreme_tilt"] or bias == "contrarian" or drop
                    else None
                )
                view = symbol_views.get(sym)
                lean_on = s.get("band_lean", True)
                # Which way to lean, first match wins: the book outside its band, your view on
                # this underlying, a price extreme (against the move), your bias in the band.
                if band_out and lean_on:
                    tilt, source = band_out, "band"
                elif view in targets.VIEWS:
                    tilt, source = targets.VIEW_TILT[view], "view"
                elif ext_dir:
                    tilt, source = ext_dir, "extreme"
                elif bias_in and lean_on:
                    tilt, source = bias_in, "bias"
                else:
                    tilt, source = None, None
                leaned = source in ("band", "bias")
                # Would own: a cash-secured short put, the premium-collecting way to buy.
                own = source == "view" and view == "would_own"
                n = max(1, s["contracts"] // 2) if careful else s["contracts"]
                if account in self.brokers:
                    n = min(n, (await self.brokers[account].config())["max_contracts"])
                # Undefined risk first when allowed; over the notional limit, the same idea as
                # a defined-risk spread instead.
                attempts = [True] if own else [True, False] if strangle else [False]
                legs = pv = None
                for undefined in attempts:
                    if tilt and source in ("view", "extreme"):
                        # A view on the stock or a sharp move: one side only, outside the
                        # expected move, so the trade does not depend on calling the turn.
                        legs, label = one_side(
                            rows,
                            vol["spot"],
                            vol["em"],
                            "put" if tilt == "down" else "call",
                            undefined,
                            s,
                            _mid,
                        )
                    else:
                        legs, label = build_trade(rows, undefined, s, tilt)
                    if legs is None:
                        break
                    body = {
                        "account": account,
                        "intent": "open",
                        "duration": "DAY",
                        "profit_target": rules.profit_target,
                        "legs": [{**x, "quantity": x["quantity"] * n} for x in legs],
                    }
                    try:
                        pv = await auto.submit({**body, "limit_net": None}, False)
                    except ValueError as e:
                        label, legs = f"preview failed: {e}", None
                        break
                    strangle = undefined
                    if undefined and any("notional limit" in e for e in pv["errors"]):
                        label += " (over the notional limit: trying a spread)"
                        continue
                    break
                if legs is None or pv is None:
                    skipped.append({"symbol": sym, "reason": label})
                    continue
                if own:
                    # Cash-secured only: cash and sweep, after the other Would own puts.
                    need = sum(
                        float(parse_option_symbol(x["symbol"]).strike) * 100 * -x["quantity"]
                        for x in body["legs"]
                        if x["quantity"] < 0
                    )
                    left = cash_mod.free_cash(acct) - own_reserved - cash_mod.FEE_BUFFER
                    if need > left + 1e-9:
                        skipped.append(
                            {
                                "symbol": sym,
                                "reason": f"Would own: not cash-secured: the put needs "
                                f"{need:,.0f} and {max(left, 0):,.0f} of cash and sweep is "
                                "free after your other Would own puts (never margin)",
                            }
                        )
                        continue
                    own_reserved += need
                problems = list(pv["errors"]) + list(pv.get("live_blocks") or [])
                bp = pv.get("bp_effect")
                bp_cap = s["max_bp_share"] * (0.5 if careful else 1.0)
                if bp is not None and net_liq and bp > bp_cap * net_liq:
                    problems.append(f"buying power {bp:,.0f} is over {bp_cap:.1%} of net liq")
                after = pv.get("bp_used_after")
                if after is not None and ceiling is not None and after > ceiling:
                    problems.append(
                        f"buying power in use would be {after:.0%}, over the {ceiling:.0%} "
                        "ceiling for this VIX"
                    )
                ba = pv.get("band_after") or {}
                tb = bands.get("theta") or {}
                if ba.get("theta") is not None and tb.get("max") is not None:
                    if ba["theta"] > tb["max"] + 1e-12:
                        problems.append(
                            f"daily theta would be {ba['theta']:.2%} of net liq, over the "
                            f"{tb['max']:.2%} maximum"
                        )
                if problems:
                    skipped.append({"symbol": sym, "reason": "; ".join(problems)})
                    continue
                pop = (pv.get("entry") or {}).get("pop")
                if pop is not None and pop < s["min_pop"]:
                    skipped.append(
                        {
                            "symbol": sym,
                            "reason": f"probability of profit {pop:.0%}, under {s['min_pop']:.0%}",
                        }
                    )
                    continue
                push = _delta_push(pv)
                candidates.append(
                    {
                        "symbol": sym,
                        "ivr": round(ivr, 1),
                        "expiration": exp["date"],
                        "dte": exp["dte"],
                        "label": label,
                        "strategy": label.split(" ")[0] + " " + label.split(" ")[1]
                        if label.startswith(
                            ("Short put", "Short call", "Put spread", "Call spread")
                        )
                        else "Strangle"
                        if strangle
                        else "Iron condor",
                        "mid": pv["prices"]["mid"],
                        "bp": bp,
                        "pop": (pv.get("entry") or {}).get("pop"),
                        "body": body,
                        "extreme": ext,
                        "tilt": tilt,
                        "lean_source": source,
                        "drop_signal": drop,
                        "caution": vol["caution"],
                        "skew": vol["skew"],
                        # The view the trade is managed with once open (its rolls lean this way).
                        "view": "would_own" if own else targets.TILT_VIEW[tilt],
                        "band_lean": leaned,
                        "delta_push": push,
                        "iv30": vol["iv30"],
                        "hv": vol["hv"],
                        "score": _score(
                            ivr,
                            pv["prices"]["mid"],
                            n,
                            bp,
                            pop,
                            vol["iv30"] / vol["hv"] if vol["iv30"] and vol["hv"] else None,
                        ),
                    }
                )
                volatility = (
                    f"IV rank {ivr:.0f} · IV30 {vol['iv30']:.0f}%"
                    if vol["iv30"] is not None
                    else f"IV rank {ivr:.0f} · IV30 unknown"
                )
                if vol["iv30"] is not None and vol["hv"]:
                    volatility += f" vs realized {vol['hv']:.0f}%"
                if vol["rich_only"]:
                    volatility += (
                        f": IV rank under {s['min_ivr']} but IV30 rich, so an iron condor "
                        "(defined risk)"
                    )
                elif defined_only and ivr >= s["undefined_ivr"]:
                    volatility += f"; VIX {vix:.0f}: defined risk only"
                candidates[-1]["sector"] = sector
                candidates[-1]["volatility"] = volatility
                if tilt:
                    lean = "bullish" if tilt == "down" else "bearish"
                    if leaned:
                        why_lean = targets.lean_reason(bands)
                    elif source == "view":
                        why_lean = f"your view on {sym}"
                    else:
                        why_lean = "against the move: " + "; ".join(ext["reasons"])
                    candidates[-1]["label"] = f"{label}, tilted {lean}: {why_lean}"
                elif source == "view":
                    candidates[-1]["label"] = f"{label}, symmetric: your neutral view on {sym}"
                if push:
                    candidates[-1]["label"] += f". Warning: {push}"
                div = dividends.info(quotes.get(sym), now.date(), sym)
                if div and div["ex_date"] <= exp["date"]:
                    # Out of the money at entry, so no early assignment yet; Positions flags
                    # the short call if it goes in the money near the date.
                    candidates[-1]["ex_dividend"] = div
                    candidates[-1]["label"] += (
                        f". Ex-dividend {'about ' if div['estimated'] else ''}{div['ex_date']} "
                        f"({div['amount']:.2f}) falls inside the trade"
                    )
                if drop:
                    candidates[-1]["label"] = (
                        f"Drop signal (IV rank {ivr:.0f}, put skew {vol['skew']:.1f} pts): "
                        + candidates[-1]["label"]
                    )
                if careful:
                    candidates[-1]["label"] += ". Defined risk, half size: " + "; ".join(
                        vol["caution"]
                    )
                if vol["skew"] is not None:
                    candidates[-1]["volatility"] += f" · skew {vol['skew']:.1f}"
                candidates[-1]["label"] += f" ({candidates[-1]['volatility']})"
            acted = []
            if act and level != "off":
                # Trades that keep delta in its band first, then those leaning (back toward the
                # band or against a price extreme), then by IV rank.
                order = sorted(
                    candidates,
                    key=lambda c: (
                        bool(c.get("delta_push")),
                        not c.get("drop_signal"),
                        not c.get("tilt"),
                        -c["score"],
                    ),
                )
                run_sector = dict(by_sector)
                for c in order:
                    if placed_today >= s["max_new_per_day"]:
                        c["note"] = "daily limit of new trades reached"
                        continue
                    sec = c.get("sector")
                    if (
                        sec
                        and s.get("max_per_sector")
                        and run_sector.get(sec, 0) >= s["max_per_sector"]
                    ):
                        c["note"] = (
                            f"{_sector_name(sec)} would go over {s['max_per_sector']} trades "
                            "per sector"
                        )
                        continue
                    if net_liq and c["bp"] and bp_today + c["bp"] > s["max_bp_day"] * net_liq:
                        c["note"] = (
                            f"would take buying power added today over {s['max_bp_day']:.0%} "
                            "of net liq"
                        )
                        continue
                    what = await self._act(account, level, c, s, today)
                    if what:
                        acted.append(what)
                        if sec:
                            run_sector[sec] = run_sector.get(sec, 0) + 1
                        placed_today += 1
                        bp_today += c["bp"] or 0
            result = {
                "at": time.time(),
                "day": today if act else prev.get("day"),
                "new_today": placed_today,
                "bp_today": bp_today,
                "level": level,
                "candidates": candidates,
                "skipped": skipped,
                "acted": acted,
            }
            st[account] = result
            await self.store.set_setting(STATE_KEY, st)
            return result

    async def _extreme(self, sym: str, quotes: dict, s: dict) -> dict | None:
        if self.history is None:
            return None
        closes = await self.history.ensure(sym)
        q = (quotes.get(sym) or {}).get("quote", {})
        price = next((float(q[k]) for k in ("mark", "lastPrice") if q.get(k)), None)
        change = q.get("netPercentChange")
        return compute_extreme(
            closes or [], price, None if change is None else float(change), await self._iv30(sym), s
        )

    def _sector(self, sym: str) -> str:
        """The sector, for spreading trades; a stock of unknown sector is its own."""
        industries = getattr(self.earnings, "industries", None) or {}
        sec = portfolio_view.sector(sym, industries)
        return f"{sec}:{sym}" if sec == portfolio_view.UNKNOWN else sec

    async def _iv30(self, sym: str) -> float | None:
        cache = getattr(self.watchlist, "cache", {}) or {}
        iv = (cache.get(sym) or {}).get("iv30")
        if iv is None and sym == "SPY":
            iv = ((self.portfolios["live"].snapshot or {}).get("market") or {}).get("vix")
        return iv

    async def _check(self, account, sym, s, held, quotes):
        """(IV rank, expiration, chain rows) when the underlying qualifies, else the reason."""
        if held.get(sym, 0) >= s["max_per_underlying"]:
            return f"already {held[sym]} open trade{'s' if held[sym] > 1 else ''} here"
        ivr = await self._ivr(account, sym)
        if ivr is None:
            return "no IV rank yet (it needs IV history)"
        iv30 = await self._iv30(sym)
        hv, closes = None, []
        if self.history is not None:
            try:
                closes = await self.history.ensure(sym) or []
                hv = realized_vol(closes)
            except Exception as e:  # history is a refinement, never a block
                log.warning("Scanner history for %s failed: %s", sym, e)
        why, rich_only = vol_check(ivr, iv30, hv, s)
        if why:
            return why
        q = quotes.get(sym) or {}
        kind = {q.get("assetMainType"), q.get("assetSubType"), q.get("assetType")}
        fund = bool(kind & FUNDS) or sym.startswith("$")
        if not fund and self.earnings is not None and self.earnings.is_fund(sym):
            fund = True
        if not fund:
            # The earnings buffer: a stock opens only when its next report is known and far
            # enough away.
            info = self.earnings.info(sym) if self.earnings is not None else None
            if info is None:
                return (
                    "single stock with no earnings date found, so the earnings buffer cannot "
                    "be checked (add it under Settings, Earnings dates)"
                )
            if info["sessions_left"] <= s["earnings_days"]:
                return (
                    f"earnings {info['date']} {info['label']}: within the "
                    f"{s['earnings_days']}-session buffer"
                )
            report = info["date"]
        else:
            report = None
        today = self.clock().astimezone(ET).date()
        exps = chain_mod.expirations(await self.source.expirations(sym), today)
        exp = pick_expiration(exps, s)
        if exp is None:
            return (
                f"no {'monthly ' if s['monthly_only'] else ''}expiration in "
                f"{s['min_dte']}-{s['max_dte']} DTE"
            )
        caution = []
        if report and s.get("avoid_through_earnings", True) and report <= exp["date"]:
            # A report inside the trade also lifts IV rank: the event, not volatility that is
            # expensive in the usual sense.
            why_e = (
                f"earnings {report} fall before the {exp['date']} expiration (and the report "
                "inflates IV rank)"
            )
            if s.get("earnings_inside", "skip") == "skip":
                return why_e
            caution.append(why_e)
        d = date.fromisoformat(exp["date"])
        chain = chain_mod.chain_rows(
            await self.source.option_chain(sym, d, d, strike_count=None) or {}
        )
        rows, spot = chain["rows"], chain["underlying_price"]
        if not rows or not spot:
            return "no option chain"
        atm = min(rows, key=lambda r: abs(r["strike"] - spot))
        spreads, oi = [], 0.0
        for c in (atm["call"], atm["put"]):
            if c and c["bid"] is not None and c["ask"] and c["mark"]:
                spreads.append((c["ask"] - c["bid"]) / c["mark"])
            oi += (c or {}).get("oi") or 0
        if spreads and sum(spreads) / len(spreads) > s["max_spread"]:
            return f"at-the-money spread {sum(spreads) / len(spreads):.1%} is too wide"
        if oi < s["min_oi"]:
            return f"at-the-money open interest {oi:,.0f}, under {s['min_oi']:,}"
        today_ = self.clock().astimezone(ET).date()
        iv_hist = []
        try:
            iv_hist = await self.store.iv_history(sym, date.fromordinal(today_.toordinal() - 40))
        except Exception as e:  # a refinement, never a block
            log.warning("Scanner IV history for %s failed: %s", sym, e)
        last = (
            self.earnings.last_report(sym)
            if self.earnings is not None and hasattr(self.earnings, "last_report")
            else None
        )
        change = (q.get("quote") or {}).get("netPercentChange")
        shocked = shock(
            closes,
            iv_hist,
            last,
            s,
            None if change is None else float(change),
            today_,
        )
        if shocked:
            caution.append(shocked)
        return (
            ivr,
            exp,
            rows,
            {
                "iv30": iv30,
                "hv": hv,
                "rich_only": rich_only,
                "spot": spot,
                "em": chain.get("expected_move"),
                "skew": skew(rows),
                "caution": caution,
            },
        )

    async def _act(self, account, level, c, s, today) -> str | None:
        auto = self.automations[account]
        key = f"scan:{account}:{c['symbol']}:{today}"
        async with auto.lock:
            st = await auto.load()
            if key in st["acted"] or any(p["key"] == key for p in st["proposals"]):
                return None
            if level == "auto" and c.get("delta_push"):
                # Against the portfolio's delta band: never opened without your tap.
                level = "propose"
            if level == "auto" and auto.blocked():
                # Paused, stopped by the daily loss limit, or the sign-in is lapsing.
                level = "propose"
            title = f"{c['symbol']} {c['strategy']}"
            reason = (
                f"IV rank {c['ivr']:.0f}: {c['label']}, {c['expiration']} ({c['dte']} DTE), "
                f"about {abs(c['mid'] or 0):.2f} credit"
            )
            if level == "notify":
                if await auto._notify(key, f"Scanner: {title}", reason):
                    auto._log(None, "scan", f"Found: {title}: {reason}")
                    await auto._save()
                    return f"notify {c['symbol']}"
                return None
            if level == "propose":
                st["proposals"].append(
                    {
                        "id": f"p{int(time.time() * 1000)}{len(st['proposals'])}",
                        "key": key,
                        "trade_id": f"new:{c['symbol']}",
                        "rule": "scan",
                        "kind": "open",
                        "underlying": c["symbol"],
                        "strategy": c["strategy"],
                        "reason": f"New trade. {reason}",
                        "bodies": [c["body"]],
                        "status": "pending",
                        "created_at": time.time(),
                        "due_at": None,
                        "automanage": s["automanage_new"],
                        "view": c.get("view"),
                    }
                )
                auto._log(None, "scan", f"Proposed: {title}: {reason}")
                await auto._notify(
                    f"prop:{key}",
                    f"Scanner: {title}",
                    f"Proposed: {reason}. Open the app to review.",
                )
                await auto._save()
                return f"propose {c['symbol']}"
            # Auto (the Schwab account only with LIVE_AUTO and Manual and automatic orders).
            st["acted"][key] = time.time()
            done: list[str] = []
            wide = await auto._place_all(
                f"new:{c['symbol']}",
                Action("scan", "open", reason),
                [c["body"]],
                await auto.settings(),
                done,
                automanage=s["automanage_new"],
                view=c.get("view"),
            )
            if wide == "wide":
                st["acted"].pop(key, None)  # the next scan may try again
            await auto._save()
            return f"place {c['symbol']}" if done else None
