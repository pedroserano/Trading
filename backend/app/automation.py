"""Automanage: the management rules applied to positions on their own.

Each open trade can be switched to automanage. Each rule then acts at one of four levels:
off, notify, propose (a ready ticket waits for one tap) or auto (the app places the order).
Orders go through exactly the same ticket, guards and preview as manual ones; this module
only decides when to act and how far to chase the price.

Phase 4 runs it on the simulated account. See PLAN.md section 11.
"""

import asyncio
import copy
import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import reduce
from zoneinfo import ZoneInfo

from . import chain as chain_mod
from .strategies import CALENDARS, RATIOS, not_tasty, target_for

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
STATE_KEY = "automation"
SETTINGS_KEY = "automation_settings"
LEVELS = ("off", "notify", "propose", "auto")
RULES = {
    "profit": "Profit target",
    "dte": "Manage at DTE",
    "tested": "Tested short strike",
    "loss": "Loss at a multiple of credit",
    "expiry": "Expiration day, short in the money",
    "earnings": "Earnings ahead (single stocks)",
    "dividend": "Ex-dividend, short call likely assigned",
}
DEFAULT_SETTINGS = {
    "levels": {
        "profit": "auto",
        "dte": "propose",
        "tested": "notify",
        "loss": "notify",
        "expiry": "propose",
        "earnings": "notify",
        "dividend": "notify",
    },
    "dte_defined_action": "close",  # or "roll"
    "roll_target_dte": 45,
    # tastytrade keeps rolling for a credit while premium is worth selling; IV rank decides.
    "min_roll_ivr": 25,
    "max_rolls": 0,  # 0: no limit
    "allow_inverted": False,  # rolling the untested side past the tested strike
    # A tested side, the tastytrade way: roll the untested side closer for a credit.
    "untested_target": "match",  # "match": the tested side's delta; "fixed": the tested delta
    "roll_ic_untested": True,  # iron condors roll their untested vertical, same width
    "max_untested_rolls": 2,  # per expiration; then the DTE rule takes over
    "roll_single_itm": False,  # a lone short put or call in the money rolls out for a credit
    # An inverted strangle (the guts): buy the guts, sell the wings, in the same expiration.
    "guts_to_wings": True,
    "wings_delta": 0.16,
    "guts_extra_debit": 0.25,  # per unit, on top of the inversion width
    # A straddle, or a strangle whose strikes are close together, cannot roll its untested
    # side much closer; once a short is deeply tested it gets the same guts-to-wings repair.
    "guts_near": True,
    "guts_near_gap": 0.01,  # strangle strikes at most this share of the price apart
    "guts_near_delta": 0.70,  # a short this deep acts; below it the usual tested rules
    # Close single-stock trades this many sessions before the last one before the report
    # (0: on that last session itself).
    "earnings_close_sessions": 1,
    "min_roll_credit": 0.0,  # per unit; a roll for less is replaced by a close
    "step_cents": 1,
    "step_minutes": 3,
    "max_concession": 0.25,  # share of the distance from mid to natural
    "grace_minutes": 0,
    "daily_cap": 10,  # automatic orders per day
    # Stop all automation for the rest of the day once net liq is down this share since the
    # day began (0: off).
    "daily_loss_limit": 0.02,
    # Skip an automatic order, and try again later, while its bid/ask is wider than this
    # share of its mid (and more than 10 cents).
    "max_order_spread": 0.25,
    "window_start": "09:45",
    "window_end": "15:30",
    # Expiration day: shorts still in the money are closed from this time (ET), not the
    # moment they are touched; a 0 DTE trade is on its expiration day from the open.
    "expiry_close_at": "15:45",
    # ...and so are shorts out of the money but this close to their strike (share of the
    # price), which could finish in the money and be assigned.
    "expiry_pin_margin": 0.0,
    # The order ticket: "Automanage on fill" starts ticked for new trades.
    "ticket_automanage": True,
}
# Adjustable on the Settings page: (key, label, low, high, kind, unit).
SETTING_FIELDS = [
    ("roll_target_dte", "Roll out to about", 14, 120, "int", "DTE"),
    ("min_roll_ivr", "Roll only if IV rank is at least; else close", 0, 100, "int", "IVR"),
    ("max_rolls", "Close instead of rolling after (0: no limit)", 0, 20, "int", "rolls"),
    ("max_untested_rolls", "Untested-side rolls per expiration, at most", 0, 10, "int", "rolls"),
    ("wings_delta", "Guts to wings: sell the new wings at", 0.05, 0.5, "delta", "Δ"),
    (
        "guts_near_gap",
        "Narrow strangle: strikes at most this far apart, share of the price",
        0,
        10,
        "percent",
        "%",
    ),
    (
        "guts_near_delta",
        "Straddle or narrow strangle: buy the guts once a short reaches",
        0.4,
        0.95,
        "delta",
        "Δ",
    ),
    (
        "guts_extra_debit",
        "Guts to wings: pay at most the in-the-money value plus",
        0.0,
        10.0,
        "float",
        "$ per unit",
    ),
    (
        "earnings_close_sessions",
        "Close before earnings, sessions ahead of the report (0: the last session)",
        0,
        20,
        "int",
        "sessions",
    ),
    ("min_roll_credit", "Least credit to roll for, per unit", 0.0, 50.0, "float", "$"),
    ("step_cents", "Price step", 1, 25, "int", "cents"),
    ("step_minutes", "Step every", 1, 60, "int", "min"),
    ("max_concession", "Chase from mid toward natural, at most", 0, 100, "percent", "%"),
    ("grace_minutes", "Wait before an automatic order", 0, 120, "int", "min"),
    ("daily_cap", "Automatic orders per day, at most", 1, 100, "int", "orders"),
    (
        "daily_loss_limit",
        "Stop for the day once net liq is down (0: off)",
        0,
        20,
        "percent",
        "%",
    ),
    (
        "expiry_pin_margin",
        "Expiration day: also close shorts this close to their strike",
        0,
        2,
        "percent",
        "% of price",
    ),
    (
        "max_order_spread",
        "Skip while the order's bid/ask is wider than",
        1,
        200,
        "percent",
        "% of mid",
    ),
]
EARLY_CLOSE_STOP = (12, 30)  # the market closes at 13:00 ET on early-close days
EARLY_CLOSE_EXPIRY = (12, 45)
MAX_LEGS = 4
STRANGLES = ("Strangle", "Straddle")


def merged_settings(saved: dict | None) -> dict:
    s = copy.deepcopy(DEFAULT_SETTINGS)
    for k, v in (saved or {}).items():
        if k == "levels" and isinstance(v, dict):
            s["levels"].update({r: lv for r, lv in v.items() if r in RULES and lv in LEVELS})
        elif k in s:
            s[k] = v
    return s


def parse_settings(form: dict, current: dict) -> dict:
    """Validates the Settings form. Raises ValueError with a message for the page."""
    out = merged_settings(current)
    for rule in RULES:
        v = form.get(f"level_{rule}")
        if v is not None:
            if v not in LEVELS:
                raise ValueError(f"{RULES[rule]}: unknown level")
            out["levels"][rule] = v
    if form.get("dte_defined_action") in ("close", "roll"):
        out["dte_defined_action"] = form["dte_defined_action"]
    if form.get("_form") == "automation":  # checkboxes are only sent when ticked
        out["allow_inverted"] = form.get("allow_inverted") in ("on", "true", "1")
        out["roll_ic_untested"] = form.get("roll_ic_untested") in ("on", "true", "1")
        out["roll_single_itm"] = form.get("roll_single_itm") in ("on", "true", "1")
        out["guts_to_wings"] = form.get("guts_to_wings") in ("on", "true", "1")
        out["guts_near"] = form.get("guts_near") in ("on", "true", "1")
        out["ticket_automanage"] = form.get("ticket_automanage") in ("on", "true", "1")
    if form.get("untested_target") in ("match", "fixed"):
        out["untested_target"] = form["untested_target"]
    for key, label, lo, hi, kind, _unit in SETTING_FIELDS:
        if key not in form:
            continue
        try:
            v = float(form[key])
        except ValueError as e:
            raise ValueError(f"{label}: not a number") from e
        if not lo <= v <= hi:
            raise ValueError(f"{label}: must be between {lo:g} and {hi:g}")
        out[key] = v / 100 if kind == "percent" else (int(v) if kind == "int" else v)
    for key in ("window_start", "window_end"):
        if key in form:
            try:
                h, m = _hm(form[key])
                assert 9 <= h <= 16 and 0 <= m < 60
            except (ValueError, AssertionError):
                raise ValueError("Trading window: times between 09:30 and 16:00") from None
            out[key] = f"{h:02d}:{m:02d}"
    if not ("09:30" <= out["window_start"] < out["window_end"] <= "16:00"):
        raise ValueError("Trading window: start before end, within 09:30 to 16:00")
    if "expiry_close_at" in form:
        try:
            h, m = _hm(form["expiry_close_at"])
            assert (15, 0) <= (h, m) < (16, 0)
        except (ValueError, AssertionError):
            raise ValueError("Expiration day close: a time from 15:00 to 15:59") from None
        out["expiry_close_at"] = f"{h:02d}:{m:02d}"
    return out


def rule_hints(rules, settings: dict) -> dict[str, str]:
    """What sets off each automanage rule and what it does then, with the current values,
    for the (i) beside its level on Settings."""

    def pct(v: float) -> str:
        return f"{v * 100:g}%"

    defined = (
        "closed" if settings["dte_defined_action"] == "close" else "rolled out like any other trade"
    )
    closes = [f"IV rank is under {settings['min_roll_ivr']}"]
    if settings["max_rolls"]:
        closes.append(f"it has been rolled {settings['max_rolls']} times")
    if settings["min_roll_credit"]:
        closes.append(f"the roll pays less than {settings['min_roll_credit']:g} a unit")
    margin = settings.get("expiry_pin_margin") or 0
    sessions = settings.get("earnings_close_sessions", 1)
    return {
        "profit": (
            f"When: credit trades at {pct(rules.profit_target)} of the credit (strangles, "
            "short puts and calls, verticals, iron condors, jade lizards); straddles and iron "
            f"flies at {pct(rules.profit_target_straddle)}; front ratios at "
            f"{pct(rules.profit_target_ratio)} of their max profit, the width plus the credit "
            f"(Tony Battista, tastylive: at least 10%); calendars and diagonals at "
            f"{pct(rules.profit_target_calendar)} of the debit; a covered call at "
            f"{pct(rules.profit_target)} of the call's credit, closing the call and keeping the "
            "shares. All set on the Rules tab. Debit verticals and long options are not "
            "tastylive trades and are left alone. Does: closes the trade. At Auto a GTC closing "
            "order rests at that price from the start; at Propose or Notify you hear once it "
            "is reached."
        ),
        "dte": (
            f"When: {rules.manage_dte} DTE or less (Rules tab; for calendars, the front "
            "month). Does: calendars and diagonals are closed, never rolled; a defined-risk "
            f"trade is {defined}; an undefined-risk trade (a covered call: the call only) "
            "rolls out to about "
            f"{settings['roll_target_dte']} DTE at the same strikes for a credit, or closes "
            f"instead when {' or '.join(closes)}. Details under Rolling."
        ),
        "tested": (
            f"When: a short strike reaches {rules.tested_delta:.2f} delta or goes in the money "
            "(Rules tab). Does: a strangle rolls its untested side closer for a credit, at "
            f"most {settings['max_untested_rolls']} times per expiration; an iron condor "
            "rolls its untested vertical; an inverted strangle, or a deeply tested straddle "
            'or narrow strangle, buys the guts and sells the wings. With "Short option in the '
            'money: roll out for a credit" ticked, these also roll out in time for a credit '
            "once in the money: a single short option, a front ratio (the whole ratio), a "
            "covered call (the call; the shares stay) and a jade lizard's put (the call spread "
            "stays: it holds no upside risk while the credit covers its width). Anything else "
            "is a notification. Details under Tested side and Guts to wings."
        ),
        "loss": (
            f"When: an undefined-risk trade is down {rules.loss_multiple:g}x the credit it "
            "took in (Rules tab). Does: closes the whole trade. Defined-risk trades are not "
            "checked: their loss is already capped."
        ),
        "expiry": (
            f"When: from {settings.get('expiry_close_at', '15:45')} ET on expiration day, a "
            "short still in the money"
            + (f" or within {pct(margin)} of its strike" if margin else "")
            + ". Not the moment a strike is touched: a 0 DTE trade is managed on where it "
            "finishes. Does: closes the trade, so nothing is assigned at 4:00. Runs past the "
            "trading window until the close."
        ),
        "earnings": (
            "When: a single-stock trade that expires after the next earnings report, "
            + (
                "on the last session before it"
                if not sessions
                else f"{sessions} session{'s' if sessions != 1 else ''} before the last "
                "session ahead of it"
            )
            + ". Does: closes the trade. Dates come from Finnhub or the dates entered under "
            "Earnings; ETFs and indexes have none."
        ),
        "dividend": (
            "When: a short call in the money, expiring after the ex-dividend date, with less "
            "time value left than the dividend: it is likely to be assigned the day before. "
            "Flagged up to 10 days ahead. Does: closes the trade by the last session before "
            "the ex-date."
        ),
    }


def _hm(text: str) -> tuple[int, int]:
    h, m = str(text).split(":")
    return int(h), int(m)


def _thanksgiving(year: int) -> date:
    d = date(year, 11, 1)
    first_thu = d + timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + timedelta(weeks=3)


def early_close(d: date) -> bool:
    """NYSE's regular early closes (13:00 ET): the day before Independence Day, the day after
    Thanksgiving, and Christmas Eve, when they are trading days."""
    if d.weekday() >= 5:
        return False
    if d == _thanksgiving(d.year) + timedelta(days=1):
        return True
    if (d.month, d.day) == (12, 24):
        return True
    # July 3, unless it is the observed holiday itself (July 4 on a Saturday).
    return (d.month, d.day) == (7, 3) and date(d.year, 7, 4).weekday() != 5


def in_window(now: datetime, settings: dict) -> bool:
    et = now.astimezone(ET)
    if et.weekday() >= 5:
        return False
    t = (et.hour, et.minute)
    end = _hm(settings["window_end"])
    if early_close(et.date()):
        end = min(end, EARLY_CLOSE_STOP)
    return _hm(settings["window_start"]) <= t < end


def in_expiry_span(now: datetime, settings: dict) -> bool:
    """From the expiration-day close time to the market close (13:00 on early-close days)."""
    et = now.astimezone(ET)
    if et.weekday() >= 5:
        return False
    start = _hm(settings.get("expiry_close_at", "15:45"))
    end = (16, 0)
    if early_close(et.date()):
        start, end = min(start, EARLY_CLOSE_EXPIRY), (13, 0)
    return start <= (et.hour, et.minute) < end


@dataclass
class Action:
    rule: str
    kind: str  # target, close, roll, roll_untested, notify
    reason: str


# Deciding --------------------------------------------------------------------------------


def decide(
    st: dict,
    spot: float | None,
    rules,
    settings: dict,
    levels: dict,
    ivr: float | None = None,
    earnings: dict | None = None,
    now: datetime | None = None,
) -> list[Action]:
    """What the rules call for on one strategy (the Positions snapshot's JSON), by priority.

    Pure: no orders, no state. Levels decide later whether an action is only a
    notification, a proposal or an order. With now, the expiration-day rule waits for its
    close time; without it (tests, previews) it acts at any time.
    """
    n = st["numbers"]
    shorts = [x for x in st["legs"] if x["kind"] == "OPTION" and x["quantity"] < 0]
    out: list[Action] = []
    if not shorts:
        return out
    dte = n["dte"]
    flags = {f["kind"] for f in st["flags"]}
    itm = any(
        spot is not None
        and x["strike"] is not None
        and (spot < x["strike"] if x["put_call"] == "P" else spot > x["strike"])
        for x in shorts
    )
    margin = settings.get("expiry_pin_margin", 0.0) or 0.0
    near = (
        not itm
        and margin > 0
        and any(
            spot is not None
            and x["strike"] is not None
            and abs(spot - x["strike"]) <= spot * margin
            for x in shorts
        )
    )
    if (
        levels.get("expiry", "off") != "off"
        and dte == 0
        and (itm or near)
        and (now is None or in_expiry_span(now, settings))
    ):
        out.append(
            Action(
                "expiry",
                "close",
                "Expiration day with a short strike in the money"
                if itm
                else "Expiration day with a short strike near the money: close before "
                "it can be assigned",
            )
        )
    if (
        levels.get("earnings", "off") != "off"
        and earnings
        and 0 <= earnings["sessions_left"] <= settings.get("earnings_close_sessions", 1)
        and date.fromisoformat(earnings["date"]) <= _exp(st)
    ):
        out.append(
            Action(
                "earnings",
                "close",
                f"Earnings {earnings['date']} {earnings['label']}: close by {earnings['exit_by']}",
            )
        )
    if levels.get("dividend", "off") != "off" and "dividend" in flags:
        f = next(f for f in st["flags"] if f["kind"] == "dividend")
        out.append(
            Action(
                "dividend",
                "close",
                "Short call likely assigned before the ex-dividend date: "
                f"close by {f.get('act_by')}",
            )
        )
    if levels.get("loss", "off") != "off" and "loss" in flags:
        out.append(Action("loss", "close", f"Loss reached {rules.loss_multiple:g}x the credit"))
    if levels.get("dte", "off") != "off" and dte is not None and 0 < dte <= rules.manage_dte:
        if st["name"] in CALENDARS:
            out.append(
                Action(
                    "dte", "close", f"Front month at {dte} DTE: calendars are closed, not rolled"
                )
            )
        elif st["defined_risk"] and settings["dte_defined_action"] == "close":
            out.append(Action("dte", "close", f"{dte} DTE: defined-risk trades are closed"))
        elif settings["max_rolls"] and st.get("rolls", 0) >= settings["max_rolls"]:
            out.append(Action("dte", "close", f"{dte} DTE after {st['rolls']} rolls: close"))
        elif ivr is not None and ivr < settings["min_roll_ivr"]:
            out.append(
                Action(
                    "dte",
                    "close",
                    f"{dte} DTE with IV rank {ivr:.0f}, under {settings['min_roll_ivr']}: "
                    "close instead of rolling",
                )
            )
        else:
            out.append(Action("dte", "roll", f"{dte} DTE: roll out for a credit"))
    if levels.get("tested", "off") != "off" and "tested" in flags:
        name = st["name"]
        put = next((x for x in shorts if x["put_call"] == "P"), None)
        call = next((x for x in shorts if x["put_call"] == "C"), None)
        if (
            name == "Strangle"
            and put
            and call
            and put["strike"] > call["strike"]
            and settings.get("guts_to_wings", True)
        ):
            out.append(
                Action(
                    "tested",
                    "guts_to_wings",
                    f"Inverted by {put['strike'] - call['strike']:g}: buy the guts, sell the "
                    "wings to re-centre the trade",
                )
            )
        elif (near := guts_near(st, spot, put, call, settings)) is not None:
            out.append(
                Action(
                    "tested",
                    "guts_to_wings",
                    f"{name} deeply tested (short delta {near:.2f}): buy the guts, sell the "
                    "wings to re-centre the trade",
                )
            )
        elif name in STRANGLES:
            out.append(
                Action(
                    "tested",
                    "roll_untested",
                    "A short strike is tested: roll the untested side closer",
                )
            )
        elif name == "Iron condor" and settings.get("roll_ic_untested", True):
            out.append(
                Action(
                    "tested",
                    "roll_untested_vertical",
                    "A short strike is tested: roll the untested vertical closer",
                )
            )
        elif name in ("Short put", "Short call") and itm and settings.get("roll_single_itm"):
            out.append(
                Action(
                    "tested",
                    "roll",
                    "The short strike is in the money: roll out in time for a credit",
                )
            )
        elif name in RATIOS and itm and settings.get("roll_single_itm"):
            out.append(
                Action(
                    "tested",
                    "roll",
                    "The ratio's short strike is in the money: roll the ratio out in time for "
                    "a credit",
                )
            )
        elif name == "Covered call" and itm and settings.get("roll_single_itm"):
            out.append(
                Action(
                    "tested",
                    "roll_calls",
                    "The covered call is in the money: roll the call out in time for a credit; "
                    "the shares stay",
                )
            )
        elif (
            name == "Jade lizard"
            and put
            and spot is not None
            and spot < put["strike"]
            and settings.get("roll_single_itm")
        ):
            out.append(
                Action(
                    "tested",
                    "roll_puts",
                    "The lizard's short put is in the money: roll the put out in time for a "
                    "credit; the call spread stays",
                )
            )
        else:
            out.append(Action("tested", "notify", "A short strike is tested"))
    if levels.get("profit", "off") != "off" and profit_basis(st) is not None:
        if levels["profit"] == "auto":
            out.append(Action("profit", "target", "Profit target order resting at the broker"))
        elif "profit" in flags:
            out.append(Action("profit", "close", "Profit target reached"))
    return out


def guts_near(st: dict, spot, put, call, settings: dict) -> float | None:
    """The deepest short delta when a straddle, or a strangle with its strikes within the set
    share of the price, is tested past the guts-near delta; else None."""
    if not (settings.get("guts_near", True) and put and call and spot):
        return None
    if st["name"] == "Strangle":
        if call["strike"] - put["strike"] > settings.get("guts_near_gap", 0.01) * spot + 1e-9:
            return None
    elif st["name"] != "Straddle":
        return None
    deltas = [abs(x["delta"]) for x in (put, call) if x.get("delta") is not None]
    if not deltas or max(deltas) < settings.get("guts_near_delta", 0.70):
        return None
    return max(deltas)


def intrinsic(spot: float | None, put: dict, call: dict) -> float:
    """What the short put and call are worth at expiration at this price, per unit: an
    inverted strangle never less than its inversion width."""
    width = max(0.0, put["strike"] - call["strike"])
    if spot is None:
        return width
    return max(width, max(0.0, put["strike"] - spot) + max(0.0, spot - call["strike"]))


def _exp(st: dict) -> date:
    """The latest option expiration in the strategy: a report after it does not matter."""
    days = [x["expiration"] for x in st["legs"] if x["kind"] == "OPTION" and x["expiration"]]
    return date.fromisoformat(max(days)) if days else date.max


def profit_target(st: dict, rules) -> float:
    return target_for(st["name"], rules)


def target_limit(st: dict, rules) -> float | None:
    """The net per order unit that closes the trade with its profit target made: negative a
    debit to pay (credit trades), positive a credit to sell for (calendars)."""
    basis = profit_basis(st)
    if basis is None:
        return None
    target_of, opened = basis
    opts = [x for x in st["legs"] if x["kind"] == "OPTION"]
    # Prices are per unit of the order: a 1x2 ratio is one unit of one long and two shorts.
    units = reduce(math.gcd, (abs(int(x["quantity"])) for x in opts), 0) or 1
    return round((profit_target(st, rules) * target_of - opened) / (units * 100), 2)


def profit_basis(st: dict) -> tuple[float, float] | None:
    """(what the target is a share of, the net it was opened for), both in dollars, for the
    trades automanage takes profit on; None for the rest.

    Credit trades: max profit (the credit; for a ratio the width plus the credit). A covered
    call: the call's credit, the shares left out. Calendars and diagonals: the debit paid.
    Debit verticals and long options are not tastylive trades and are left alone.
    """
    n = st["numbers"]
    name, opened = st["name"], n["open_value"]
    if not_tasty(name, opened):
        return None
    if name == "Covered call":
        call = n.get("option_open_value")
        return (call, call) if call and call > 0 else None
    if name in CALENDARS:
        return (-opened, opened) if opened < 0 else None
    mp = n.get("max_profit")
    if opened > 0 or (name in RATIOS and mp):
        return (mp if mp and mp > 0 else opened, opened)
    return None


def sides(st: dict, kind: str) -> list[list[dict]]:
    """The option legs to act on, split by put and call side when one order would exceed
    four legs (a roll closes and reopens every leg, doubling the count)."""
    opts = [x for x in st["legs"] if x["kind"] == "OPTION"]
    factor = 2 if kind == "roll" else 1
    if len(opts) * factor <= MAX_LEGS:
        return [opts]
    out = []
    for pc in ("P", "C"):
        side = [x for x in opts if x["put_call"] == pc]
        if side and len(side) * factor <= MAX_LEGS:
            out.append(side)
    return out


def target_expiration(exps: list[dict], current: str, target_dte: int) -> str | None:
    later = [e for e in exps if e["date"] > current]
    monthly = [e for e in later if e["monthly"]] or later
    if not monthly:
        return None
    return min(monthly, key=lambda e: abs(e["dte"] - target_dte))["date"]


# Running ---------------------------------------------------------------------------------


class AutomationService:
    """Applies automanage to the simulated account's trades."""

    TICK_S = 30

    def __init__(
        self,
        store,
        portfolio,
        broker,
        source,
        preferences,
        notifier,
        submit,
        app_url: str = "",
        clock=lambda: datetime.now(ET),
        account: str = "sim",
        max_level: str = "auto",
    ):
        self.store = store
        self.portfolio = portfolio
        self.broker = broker
        self.source = source
        self.preferences = preferences
        self.notifier = notifier
        self.submit = submit  # async (order body dict, place: bool) -> dict
        self.app_url = app_url
        self.clock = clock
        self.account = account
        # The highest level any rule may act at here: live automanage stops at propose.
        self.max_level = max_level
        self.state_key = STATE_KEY if account == "sim" else f"{STATE_KEY}_{account}"
        self.lock = asyncio.Lock()
        self.state: dict | None = None

    # State -----------------------------------------------------------------------------

    async def load(self) -> dict:
        if self.state is None:
            self.state = await self.store.get_setting(self.state_key) or {}
        self.state.setdefault("paused", False)
        self.state.setdefault("trades", {})
        self.state.setdefault("proposals", [])
        self.state.setdefault("orders", {})
        self.state.setdefault("log", [])
        self.state.setdefault("counts", {})
        self.state.setdefault("notified", [])
        self.state.setdefault("acted", {})
        return self.state

    async def _save(self) -> None:
        self.state["log"] = self.state["log"][-500:]
        self.state["notified"] = self.state["notified"][-2000:]
        week = time.time() - 7 * 86400
        self.state["acted"] = {k: v for k, v in self.state["acted"].items() if v > week}
        self.state["proposals"] = [
            p for p in self.state["proposals"] if p["status"] == "pending" or p["created_at"] > week
        ]
        await self.store.set_setting(self.state_key, self.state)

    async def settings(self) -> dict:
        return merged_settings(await self.store.get_setting(SETTINGS_KEY))

    def _log(self, trade_id, rule: str | None, text: str, **extra) -> None:
        self.state["log"].append(
            {"at": time.time(), "trade_id": trade_id, "rule": rule, "text": text, **extra}
        )
        log.info("Automation (%s): trade %s %s: %s", self.account, trade_id, rule, text)

    async def set_trade(self, trade_id, enabled: bool) -> None:
        """Switches automanage on or off for one trade. The rules come from Settings."""
        async with self.lock:
            st = await self.load()
            st["trades"][str(trade_id)] = {"enabled": enabled}
            await self._save()

    async def watch_entry(self, order: dict) -> None:
        """An opening order placed from the ticket with "Automanage on fill" ticked: the new
        trade is switched to automanage once it fills. The order itself is left as placed."""
        async with self.lock:
            await self.load()
            self.state["orders"][str(order["id"])] = {
                "trade_id": order.get("trade_id"),
                "rule": "entry",
                "automanage_on_fill": True,
            }
            self._log(None, "entry", f"Order #{order['id']}: automanage switches on when it fills")
            await self._save()

    async def set_paused(self, paused: bool, why: str = "by you") -> None:
        async with self.lock:
            st = await self.load()
            if st["paused"] != paused:
                st["paused"] = paused
                self._log(None, None, f"Automation {'paused' if paused else 'resumed'} {why}")
                await self._save()

    async def dismiss(self, proposal_id: str) -> None:
        async with self.lock:
            st = await self.load()
            for p in st["proposals"]:
                if p["id"] == proposal_id and p["status"] == "pending":
                    p["status"] = "dismissed"
                    self._log(p["trade_id"], p["rule"], "Proposal dismissed by you")
            await self._save()

    async def view(self) -> dict:
        st = await self.load()
        settings = await self.settings()
        today = self.clock().date().isoformat()
        return {
            "paused": st["paused"],
            "settings": {**settings, "levels": self.levels(settings)},
            "max_level": self.max_level,
            "account": self.account,
            "trades": st["trades"],
            "proposals": [p for p in st["proposals"] if p["status"] == "pending"],
            "log": st["log"][-150:][::-1],
            "orders_today": st["counts"].get(today, 0),
            "in_window": in_window(self.clock(), settings),
            "early_close": early_close(self.clock().astimezone(ET).date()),
            "blocked": self.blocked(),
            "halted": st.get("halted_on") == today,
        }

    # Loop ------------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Automation tick failed")
            await asyncio.sleep(self.TICK_S)

    async def tick(self) -> list[str]:
        """One pass over every automanaged trade. Returns what was done, for tests."""
        async with self.lock:
            st = await self.load()
            settings = await self.settings()
            done: list[str] = []
            snap = self.portfolio.snapshot or {}
            now = self.clock()
            if st["paused"] or not self.broker.state:
                return done
            # After the trading window, only the expiration-day close still acts, until the
            # market closes.
            expiry_only = not in_window(now, settings) and in_expiry_span(now, settings)
            if snap.get("session") != "open" or not (in_window(now, settings) or expiry_only):
                return done
            quotes_at = (snap.get("data") or {}).get("quotes_at")
            if not quotes_at or time.time() - quotes_at > 90:
                st["paused"] = True
                self._log(None, None, "Automation paused: prices are stale")
                await self._notify(
                    "pause-stale",
                    "Automation paused",
                    "Prices stopped updating, so automation paused itself.",
                )
                await self._save()
                return ["paused: stale"]
            hold = await self._hold(snap, settings, now)
            if hold:
                await self._save()
                return [f"held: {hold}"]
            rules = await self.preferences.rules()
            await self._step_orders(settings, done, only=("expiry",) if expiry_only else None)
            open_ids = set()
            for u in snap.get("underlyings", []):
                for s in u["strategies"]:
                    tid = s.get("trade_id")
                    if tid is None:
                        continue
                    open_ids.add(str(tid))
                    if not st["trades"].get(str(tid), {}).get("enabled"):
                        continue
                    levels = self.levels(settings)
                    ivr = (u.get("ivr") or {}).get("rank")
                    acts = decide(
                        s, u["price"], rules, settings, levels, ivr, u.get("earnings"), now
                    )
                    if expiry_only:
                        acts = [a for a in acts if a.rule == "expiry"]
                    for action in acts:
                        await self._handle(s, u, action, levels[action.rule], settings, rules, done)
            # Proposals for trades that have closed no longer apply; the scanner's new
            # trades last for the day they were found.
            today = now.date().isoformat()
            for p in st["proposals"]:
                if p["status"] != "pending":
                    continue
                if p["rule"] == "scan":
                    if not p["key"].endswith(today):
                        p["status"] = "expired"
                elif p["trade_id"] not in open_ids:
                    p["status"] = "expired"
            await self._save()
            return done

    def blocked(self) -> str | None:
        """Why nothing may be placed automatically right now: paused, stopped for the day by
        the loss limit, or held while the Schwab sign-in is about to lapse."""
        st = self.state or {}
        if st.get("paused"):
            return "automation is paused"
        if st.get("halted_on") == self.clock().date().isoformat():
            return "stopped for the day: the daily loss limit was reached"
        days = ((self.portfolio.snapshot or {}).get("connection") or {}).get("expires_in_days")
        if days is not None and days < 1:
            return "the Schwab sign-in has less than a day left"
        return None

    async def _hold(self, snap: dict, settings: dict, now: datetime) -> str | None:
        """Checks the daily loss limit and the sign-in, once per tick. Returns a reason to act
        on nothing this tick, or None."""
        st = self.state
        today = now.date().isoformat()
        nl = (snap.get("account") or {}).get("net_liq")
        start = st.get("day_start") or {}
        if nl and start.get("day") != today:
            st["day_start"] = start = {"day": today, "net_liq": nl}
        limit = settings.get("daily_loss_limit") or 0
        if st.get("halted_on") == today:
            return "daily loss limit"
        if limit and nl and start.get("net_liq"):
            loss = start["net_liq"] - nl
            if loss > limit * start["net_liq"]:
                st["halted_on"] = today
                self._log(
                    None,
                    None,
                    f"Automation stopped for the day: net liq down {loss:,.0f} "
                    f"({loss / start['net_liq']:.1%}), over the {limit:.1%} daily loss limit",
                )
                await self._notify(
                    f"loss-limit:{today}",
                    "Automation stopped for the day",
                    f"Net liq is down {loss / start['net_liq']:.1%} today, over the "
                    f"{limit:.1%} daily loss limit. It starts again tomorrow.",
                )
                return "daily loss limit"
        days = (snap.get("connection") or {}).get("expires_in_days")
        if days is not None and days < 1:
            if await self._notify(
                f"signin-hold:{today}",
                "Automation on hold",
                "The Schwab sign-in has less than a day left, so automation is holding until "
                "you sign in again (Settings).",
            ):
                self._log(None, None, "Automation on hold: the Schwab sign-in is about to lapse")
            return "sign-in"
        return None

    def levels(self, settings: dict) -> dict:
        cap = LEVELS.index(self.max_level)
        return {r: LEVELS[min(LEVELS.index(lv), cap)] for r, lv in settings["levels"].items()}

    def _working(self, trade_id) -> list[dict]:
        return [
            o
            for o in (self.broker.state or {}).get("orders", [])
            if o["status"] == "WORKING" and str(o["trade_id"]) == str(trade_id)
        ]

    async def _handle(self, s, u, action: Action, level: str, settings, rules, done) -> None:
        tid = s["trade_id"]
        key = f"{tid}:{action.rule}:{s['numbers']['expiration']}:{s.get('rolls', 0)}"
        if level == "notify" or action.kind == "notify":
            if await self._notify(f"auto:{key}", f"{u['symbol']} {s['name']}", action.reason):
                self._log(tid, action.rule, f"Notified: {action.reason}")
                done.append(f"notify {action.rule}")
            return
        if action.kind == "target":
            await self._ensure_target(s, u, rules, done)
            return
        if action.kind.startswith("roll_untested"):
            made = self.state.setdefault("untested", {}).get(
                f"{tid}:{s['numbers']['expiration']}", 0
            )
            if made >= settings.get("max_untested_rolls", 2):
                if await self._notify(
                    f"untested-cap:{tid}:{s['numbers']['expiration']}",
                    f"{u['symbol']} {s['name']}",
                    f"Still tested after {made} untested-side rolls this expiration; "
                    "no more rolls until the DTE rule acts",
                ):
                    self._log(tid, action.rule, "Untested-side roll limit reached; notified")
                return
        # Close, roll and roll-untested act once per key (trade, rule, expiration, rolls),
        # and only when no automatic order is already working on the trade.
        if self._busy(tid):
            return
        acted = self.state.setdefault("acted", {})
        if key in acted:
            return
        existing = next((p for p in self.state["proposals"] if p["key"] == key), None)
        if existing is not None:
            if (
                existing["status"] == "pending"
                and existing.get("due_at")
                and time.time() >= existing["due_at"]
            ):
                existing["status"] = "placed"
                acted[key] = time.time()
                if await self._place_all(tid, action, existing["bodies"], settings, done) == "wide":
                    existing["status"] = "pending"
                    acted.pop(key, None)
            return
        bodies = await self._bodies(s, u, action, settings)
        if not bodies:
            if await self._notify(
                f"auto-none:{key}",
                f"{u['symbol']} {s['name']}",
                f"{action.reason}, but no order could be built",
            ):
                self._log(tid, action.rule, f"{action.reason}; no order could be built")
            return
        grace = int(settings.get("grace_minutes") or 0)
        if level == "propose" or (level == "auto" and grace):
            due = time.time() + grace * 60 if level == "auto" else None
            self.state["proposals"].append(
                {
                    "id": f"p{int(time.time() * 1000)}{len(self.state['proposals'])}",
                    "key": key,
                    "trade_id": str(tid),
                    "rule": action.rule,
                    "kind": action.kind,
                    "underlying": u["symbol"],
                    "strategy": s["name"],
                    "reason": action.reason,
                    "bodies": bodies,
                    "status": "pending",
                    "created_at": time.time(),
                    "due_at": due,
                }
            )
            self._log(
                tid,
                action.rule,
                f"Proposed: {action.reason}"
                + (f" (placing in {grace} min unless dismissed)" if due else ""),
            )
            await self._notify(
                f"prop:{key}",
                f"{u['symbol']} {s['name']}",
                f"Proposed: {action.reason}. Open the app to review.",
            )
            done.append(f"propose {action.rule}")
            return
        if level == "auto":
            acted[key] = time.time()
            if await self._place_all(tid, action, bodies, settings, done) == "wide":
                acted.pop(key, None)  # tried again on a later tick

    def _busy(self, tid) -> bool:
        return any(
            self.state["orders"].get(str(o["id"]), {}).get("rule") not in (None, "profit", "entry")
            for o in self._working(tid)
        )

    async def accept(self, proposal_id: str) -> list[str]:
        """Places a pending proposal now, at mid, stepping like an automatic order."""
        async with self.lock:
            st = await self.load()
            p = next((x for x in st["proposals"] if x["id"] == proposal_id), None)
            if p is None or p["status"] != "pending":
                raise ValueError("That proposal is no longer pending")
            if self._busy(p["trade_id"]):
                raise ValueError("An automatic order is already working on this trade")
            settings = await self.settings()
            done: list[str] = []
            p["status"] = "placed"
            st.setdefault("acted", {})[p["key"]] = time.time()
            self._log(p["trade_id"], p["rule"], "Proposal accepted by you")
            await self._place_all(
                p["trade_id"],
                Action(p["rule"], p.get("kind", "close"), p["reason"]),
                p["bodies"],
                settings,
                done,
                manual=True,
                automanage=p.get("automanage", False),
            )
            await self._save()
            return done

    async def _place_all(
        self, tid, action, bodies, settings, done, manual=False, automanage=False
    ) -> str | None:
        """Places each body at mid. Returns "wide" when an automatic order was skipped for
        its bid/ask spread, so the caller can try again later."""
        today = self.clock().date().isoformat()
        for body in bodies:
            if not manual and self.state["counts"].get(today, 0) >= settings["daily_cap"]:
                self.state["paused"] = True
                self._log(None, None, "Automation paused: daily order cap reached")
                await self._notify(
                    f"cap:{today}",
                    "Automation paused",
                    f"The daily cap of {settings['daily_cap']} automatic orders was reached.",
                )
                return
            try:
                preview = await self.submit({**body, "limit_net": None}, False)
                if preview["errors"]:
                    raise ValueError("; ".join(preview["errors"]))
                mid = preview["prices"]["mid"]
                natural = preview["prices"]["natural"]
                far = preview["prices"].get("far")
                if not manual and mid is not None and natural is not None and far is not None:
                    spread = abs(far - natural)
                    cap = settings.get("max_order_spread", 0.25)
                    if spread > 0.10 + 1e-9 and spread > cap * abs(mid) + 1e-9:
                        self._log(
                            tid,
                            action.rule,
                            f"Skipped for now: bid/ask {spread:.2f} is wider than "
                            f"{cap:.0%} of the {abs(mid):.2f} mid; trying again later",
                        )
                        await self._notify(
                            f"wide:{tid}:{action.rule}:{today}",
                            "Automatic order waiting",
                            f"{action.reason}, but the bid/ask ({spread:.2f}) is too wide "
                            "for now; it will try again.",
                        )
                        return "wide"
                if body.get("_max_debit") is not None:
                    # Buying the guts costs a debit; only up to their in-the-money value plus a
                    # little.
                    if mid is None or -mid > body["_max_debit"] + 1e-9:
                        cost = "unknown" if mid is None else f"{-mid:.2f}"
                        self._log(
                            tid,
                            action.rule,
                            f"Guts to wings would cost {cost}, over the "
                            f"{body['_max_debit']:.2f} limit; not placed",
                        )
                        await self._notify(
                            f"guts-cost:{tid}:{today}",
                            "Guts to wings too dear",
                            f"It would cost {cost} a unit, over the {body['_max_debit']:.2f} "
                            "limit (the in-the-money value plus your allowance).",
                        )
                        return
                elif (
                    body["intent"] == "roll"
                    and mid is not None
                    and mid < settings["min_roll_credit"]
                ):
                    # No roll for the credit wanted: close the same contracts instead.
                    self._log(
                        tid,
                        action.rule,
                        f"Roll would be {mid:+.2f}, below the {settings['min_roll_credit']:.2f} "
                        "minimum credit; closing instead",
                    )
                    body = {
                        **body,
                        "intent": "close",
                        "legs": [x for x in body["legs"] if x["effect"] == "close"],
                    }
                    preview = await self.submit({**body, "limit_net": None}, False)
                    if preview["errors"]:
                        raise ValueError("; ".join(preview["errors"]))
                    mid = preview["prices"]["mid"]
                    natural = preview["prices"]["natural"]
                if mid is None:
                    raise ValueError("No quotes for these contracts")
                order = await self.submit({**body, "limit_net": mid}, True)
            except ValueError as e:
                self.state["trades"].setdefault(str(tid), {})["enabled"] = False
                self._log(
                    tid,
                    action.rule,
                    f"Stopped: the order was refused ({e}). "
                    "Automanage switched off for this trade.",
                )
                await self._notify(f"refused:{tid}:{today}", "Automatic order refused", str(e))
                return
            self.state["counts"][today] = self.state["counts"].get(today, 0) + 1
            if action.kind.startswith("roll_untested"):
                exp_key = body.get("_expiration")
                if exp_key:
                    k = f"{tid}:{exp_key}"
                    u = self.state.setdefault("untested", {})
                    u[k] = u.get(k, 0) + 1
            self.state["orders"][str(order["id"])] = {
                "trade_id": str(tid),
                "rule": action.rule,
                "body": body,
                "mid": mid,
                "natural": natural,
                "placed_at": time.time(),
                "stepped_at": time.time(),
                # A new trade from the scanner: switch automanage on once it fills.
                "automanage_on_fill": automanage,
            }
            self._log(
                tid,
                action.rule,
                f"Placed order #{order['id']} at {mid:.2f} "
                f"({'credit' if mid > 0 else 'debit'}): {action.reason}",
                order_id=order["id"],
            )
            await self._notify(
                f"placed:{order['id']}",
                "Automatic order placed",
                f"{action.reason}. Order #{order['id']}.",
            )
            done.append(f"place {action.rule}")

    async def _ensure_target(self, s, u, rules, done) -> None:
        """Keeps one GTC closing order resting at the profit target: the net that closes the
        trade with the target share of its basis made (see profit_basis)."""
        tid = s["trade_id"]
        limit = target_limit(s, rules)
        if limit is None:
            return
        working = self._working(tid)
        if self._busy(tid):
            return  # another automatic order is working on this trade
        targets = [o for o in working if o["intent"] == "close" and o["duration"] == "GTC"]
        if targets and all(abs(o["limit_net"] - limit) < 0.005 for o in targets):
            return
        for legs in sides(s, "close"):
            body = {
                "account": self.account,
                "intent": "close",
                "trade_id": tid,
                "duration": "GTC",
                "limit_net": limit,
                "legs": [
                    {"symbol": x["symbol"], "quantity": -int(x["quantity"]), "effect": "close"}
                    for x in legs
                ],
            }
            try:
                order = await self.submit(body, True)
            except ValueError as e:
                self._log(tid, "profit", f"Profit target order refused: {e}")
                return
            self.state["orders"][str(order["id"])] = {
                "trade_id": str(tid),
                "rule": "profit",
                "body": body,
                "mid": limit,
                "natural": limit,
                "placed_at": time.time(),
                "stepped_at": time.time(),
            }
            self._log(
                tid,
                "profit",
                f"Profit target order #{order['id']} resting at {abs(limit):.2f} "
                f"{'credit' if limit > 0 else 'debit'} "
                f"({round(profit_target(s, rules) * 100)}% target)",
                order_id=order["id"],
            )
            done.append("target")

    async def _step_orders(
        self, settings: dict, done: list[str], only: tuple[str, ...] | None = None
    ) -> None:
        """Moves working automatic orders a step toward natural, up to the concession cap.
        With only, just those rules' orders move (fills are still recorded for all)."""
        every = settings["step_minutes"] * 60
        step = settings["step_cents"] / 100
        for oid, info in list(self.state["orders"].items()):
            order = next((o for o in self.broker.state["orders"] if str(o["id"]) == oid), None)
            if order is None or order["status"] != "WORKING":
                self.state["orders"].pop(oid)
                if order is not None and order["status"] == "FILLED":
                    if info.get("automanage_on_fill"):
                        new_id = order.get("trade_id")
                        if (
                            self.account == "live"
                            or new_id is None
                            or str(new_id).startswith("new:")
                        ):
                            new_id = f"c-{order['id']}"  # live trades are named by order
                        self.state["trades"][str(new_id)] = {"enabled": True}
                        self._log(new_id, info["rule"], "New trade filled; automanage on")
                    fill = order.get("fill_net")
                    self._log(
                        info["trade_id"],
                        info["rule"],
                        f"Order #{oid} filled"
                        + (
                            f" at {abs(fill):.2f} {'credit' if fill > 0 else 'debit'}"
                            if fill is not None
                            else ""
                        ),
                        order_id=order["id"],
                    )
                continue
            if info["rule"] in ("profit", "entry"):
                continue  # resting orders: never repriced
            if only is not None and info["rule"] not in only:
                continue
            if time.time() - info["stepped_at"] < every:
                continue
            mid, natural = info["mid"], info["natural"]
            floor = (
                mid - settings["max_concession"] * (mid - natural)
                if natural is not None
                else mid - 0.10
            )
            new = round(order["limit_net"] - step, 2)
            if new < floor - 1e-9:
                await self.broker.cancel(order["id"])
                self.state["orders"].pop(oid)
                self._log(
                    info["trade_id"],
                    info["rule"],
                    f"Order #{oid} not filled after stepping to the limit; cancelled",
                )
                await self._notify(
                    f"nofill:{oid}",
                    "Automatic order not filled",
                    f"Order #{oid} reached its price limit without filling and was cancelled.",
                )
                done.append(f"cancel {oid}")
                continue
            try:
                placed = await self.submit({**info["body"], "limit_net": new}, True)
            except ValueError as e:
                self._log(info["trade_id"], info["rule"], f"Could not reprice #{oid}: {e}")
                continue
            self.state["orders"].pop(oid)
            self.state["orders"][str(placed["id"])] = {**info, "stepped_at": time.time()}
            self._log(
                info["trade_id"],
                info["rule"],
                f"Repriced #{oid} to {new:.2f} as #{placed['id']}",
                order_id=placed["id"],
            )
            done.append(f"step {oid}")

    async def _bodies(self, s, u, action: Action, settings: dict) -> list[dict]:
        tid = s["trade_id"]
        base = {"account": self.account, "trade_id": tid, "duration": "DAY"}
        if action.kind == "close":
            return [
                {
                    **base,
                    "intent": "close",
                    "legs": [
                        {"symbol": x["symbol"], "quantity": -int(x["quantity"]), "effect": "close"}
                        for x in legs
                    ],
                }
                for legs in sides(s, "close")
            ]
        if action.kind in ("roll", "roll_puts", "roll_calls"):
            exps = chain_mod.expirations(
                await self.source.expirations(u["symbol"]), self.clock().date()
            )
            target = target_expiration(
                exps, s["numbers"]["expiration"], settings["roll_target_dte"]
            )
            if target is None:
                return []
            chain = chain_mod.chain_rows(
                await self.source.option_chain(
                    u["symbol"],
                    date.fromisoformat(target),
                    date.fromisoformat(target),
                    strike_count=None,
                )
                or {}
            )
            out = []
            only = {"roll_puts": "P", "roll_calls": "C"}.get(action.kind)
            groups = (
                [[x for x in s["legs"] if x["kind"] == "OPTION" and x["put_call"] == only]]
                if only
                else sides(s, "roll")
            )
            for legs in groups:
                body_legs = []
                for x in legs:
                    side = "call" if x["put_call"] == "C" else "put"
                    rows = [r for r in chain["rows"] if r[side] and r[side].get("symbol")]
                    if not rows:
                        return []
                    row = min(rows, key=lambda r: abs(r["strike"] - x["strike"]))
                    body_legs.append(
                        {"symbol": x["symbol"], "quantity": -int(x["quantity"]), "effect": "close"}
                    )
                    body_legs.append(
                        {
                            "symbol": row[side]["symbol"],
                            "quantity": int(x["quantity"]),
                            "effect": "open",
                        }
                    )
                out.append({**base, "intent": "roll", "legs": body_legs})
            return out
        if action.kind in ("roll_untested", "roll_untested_vertical"):
            return await self._untested_bodies(s, u, action, settings, base)
        if action.kind == "guts_to_wings":
            return await self._guts_bodies(s, u, settings, base)
        return []

    async def _guts_bodies(self, s, u, settings: dict, base: dict) -> list:
        """Closes the guts (an inverted strangle, a straddle, or a narrow strangle deeply
        tested) and sells a new strangle at the wings delta in the same expiration, as one
        four-leg order. The guts can never be worth less than their in-the-money value (for an
        inverted strangle, at least the inversion width), so the order may pay up to that
        value plus the set allowance."""
        shorts = [x for x in s["legs"] if x["kind"] == "OPTION" and x["quantity"] < 0]
        put = next((x for x in shorts if x["put_call"] == "P"), None)
        call = next((x for x in shorts if x["put_call"] == "C"), None)
        if not put or not call or put["quantity"] != call["quantity"]:
            return []
        exp = date.fromisoformat(put["expiration"])
        chain = chain_mod.chain_rows(
            await self.source.option_chain(u["symbol"], exp, exp, strike_count=None) or {}
        )
        target = settings.get("wings_delta", 0.16)

        def pick(side):
            rows = [r for r in chain["rows"] if r[side] and r[side].get("delta") is not None]
            return min(rows, key=lambda r: abs(abs(r[side]["delta"]) - target)) if rows else None

        new_put, new_call = pick("put"), pick("call")
        if not new_put or not new_call or new_put["strike"] >= new_call["strike"]:
            return []
        if {new_put["put"]["symbol"], new_call["call"]["symbol"]} & {put["symbol"], call["symbol"]}:
            return []
        n = int(put["quantity"])
        worth = intrinsic(u.get("price"), put, call)
        return [
            {
                **base,
                "intent": "roll",
                "legs": [
                    {"symbol": put["symbol"], "quantity": -n, "effect": "close"},
                    {"symbol": call["symbol"], "quantity": -n, "effect": "close"},
                    {"symbol": new_put["put"]["symbol"], "quantity": n, "effect": "open"},
                    {"symbol": new_call["call"]["symbol"], "quantity": n, "effect": "open"},
                ],
                "_max_debit": round(worth + settings.get("guts_extra_debit", 0.25), 2),
            }
        ]

    async def _untested_bodies(self, s, u, action: Action, settings: dict, base: dict) -> list:
        """Rolls the untested side closer for a credit: a strangle's untested short, or an
        iron condor's untested vertical at the same width. The target is the tested side's
        delta (re-centring the trade) or the fixed tested delta, and the untested short never
        crosses the tested strike unless going inverted is allowed."""
        opts = [x for x in s["legs"] if x["kind"] == "OPTION"]
        shorts = [x for x in opts if x["quantity"] < 0]
        if len(shorts) != 2 or any(x["delta"] is None for x in shorts):
            return []
        untested = min(shorts, key=lambda x: abs(x["delta"]))
        tested = next(x for x in shorts if x is not untested)
        rules = await self.preferences.rules()
        target = (
            min(abs(tested["delta"]), 0.5)
            if settings.get("untested_target", "match") == "match"
            else rules.tested_delta
        )
        exp = date.fromisoformat(untested["expiration"])
        chain = chain_mod.chain_rows(
            await self.source.option_chain(u["symbol"], exp, exp, strike_count=None) or {}
        )
        side = "call" if untested["put_call"] == "C" else "put"
        rows = {r["strike"]: r for r in chain["rows"] if r[side] and r[side].get("symbol")}
        cands = [r for r in rows.values() if r[side]["delta"] is not None]
        if not settings.get("allow_inverted"):
            cands = [
                r
                for r in cands
                if (
                    r["strike"] <= tested["strike"]
                    if side == "put"
                    else r["strike"] >= tested["strike"]
                )
            ]
        if not cands:
            return []
        best = min(cands, key=lambda r: abs(abs(r[side]["delta"]) - target))
        if best[side]["symbol"] == untested["symbol"]:
            return []
        n = int(untested["quantity"])
        legs = [
            {"symbol": untested["symbol"], "quantity": -n, "effect": "close"},
            {"symbol": best[side]["symbol"], "quantity": n, "effect": "open"},
        ]
        if action.kind == "roll_untested_vertical":
            long_leg = next(
                (x for x in opts if x["quantity"] > 0 and x["put_call"] == untested["put_call"]),
                None,
            )
            if long_leg is None:
                return []
            width = abs(untested["strike"] - long_leg["strike"])
            k = best["strike"] - width if side == "put" else best["strike"] + width
            wing = rows.get(k)
            if wing is None:
                return []  # no strike at the same width
            m = int(long_leg["quantity"])
            legs += [
                {"symbol": long_leg["symbol"], "quantity": -m, "effect": "close"},
                {"symbol": wing[side]["symbol"], "quantity": m, "effect": "open"},
            ]
        return [{**base, "intent": "roll", "legs": legs, "_expiration": s["numbers"]["expiration"]}]

    async def _notify(self, key: str, title: str, message: str) -> bool:
        """Sends a notification once per key. Returns whether it was new."""
        if key in self.state["notified"]:
            return False
        self.state["notified"].append(key)
        if self.notifier is not None and self.notifier.enabled:
            try:
                prefix = "[Simulated]" if self.account == "sim" else "[Live]"
                await self.notifier.send(f"{prefix} {title}", message, self.app_url)
            except Exception as e:
                log.warning("Automation notification failed: %s", e)
        return True
