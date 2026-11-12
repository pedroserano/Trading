"""Ex-dividend dates and early-assignment risk on short calls.

A short call in the money is likely to be assigned on the last day before the ex-dividend
date when its time value (what is left after the in-the-money amount) is less than the
dividend: exercising and collecting the dividend is then worth more than the call. American
style, physically settled options (SPY, stocks) carry this risk; cash-settled index options
(SPX, XSP, NDX, RUT) do not.

Dates come from Schwab's quote, its "fundamental" block: nextDivExDate (or divExDate when it
is still ahead) and the payment, divPayAmount (or the annual divAmount over divFreq). When
Schwab has only a past date, the next one is projected from the payment frequency and
marked as estimated.
"""

from datetime import date, timedelta

from .earnings import previous_weekday

CASH_SETTLED = {"$SPX", "SPX", "$XSP", "XSP", "$NDX", "NDX", "$RUT", "RUT", "$DJX", "DJX"}
WARN_DAYS = 10  # flag a short call this many days before the ex-date, at the most


def _day(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def info(entry: dict | None, today: date, symbol: str = "") -> dict | None:
    """The next ex-dividend date and payment from a Schwab quote entry, or None."""
    if symbol in CASH_SETTLED or symbol.startswith("$"):
        return None
    f = (entry or {}).get("fundamental") or {}
    freq = int(_num(f.get("divFreq")) or 0)
    amount = _num(f.get("divPayAmount"))
    if amount is None and _num(f.get("divAmount")) and freq:
        amount = _num(f.get("divAmount")) / freq
    if amount is None:
        return None
    estimated = False
    ex = next(
        (d for d in (_day(f.get("nextDivExDate")), _day(f.get("divExDate"))) if d and d >= today),
        None,
    )
    if ex is None:
        last = _day(f.get("divExDate"))
        if not last or not freq:
            return None
        step = timedelta(days=round(365 / freq))
        ex = last
        while ex < today:
            ex += step
        estimated = True
    return {
        "ex_date": ex.isoformat(),
        "amount": round(amount, 4),
        "estimated": estimated,
        # Exercise happens by the last session the shares still carry the dividend.
        "act_by": previous_weekday(ex).isoformat(),
    }


def at_risk(legs: list[dict], spot: float | None, div: dict | None, today: date) -> list[dict]:
    """Short calls likely to be assigned before the ex-date: in the money, expiring on or
    after it, the ex-date within WARN_DAYS, and time value under the dividend."""
    if not div or not spot:
        return []
    ex = date.fromisoformat(div["ex_date"])
    if not 0 <= (ex - today).days <= WARN_DAYS:
        return []
    out = []
    for x in legs:
        if x.get("kind") != "OPTION" or x.get("put_call") != "C" or x.get("quantity", 0) >= 0:
            continue
        if not x.get("expiration") or date.fromisoformat(str(x["expiration"])) < ex:
            continue
        itm = spot - x["strike"]
        if itm <= 0 or x.get("mark") is None:
            continue
        time_value = max(x["mark"] - itm, 0.0)
        if time_value < div["amount"]:
            out.append({"strike": x["strike"], "time_value": round(time_value, 2)})
    return out


def flag(risky: list[dict], div: dict) -> dict:
    strikes = "/".join(f"{r['strike']:g}" for r in risky)
    tv = ", ".join(f"{r['time_value']:.2f}" for r in risky)
    about = "about " if div["estimated"] else ""
    return {
        "kind": "dividend",
        "label": f"Ex-div {div['ex_date'][5:]}",
        "act_by": div["act_by"],
        "detail": (
            f"Short {strikes} call in the money with {tv} of time value, under the "
            f"{div['amount']:.2f} dividend: early assignment is likely by {div['act_by']} "
            f"(ex-dividend {about}{div['ex_date']}). Close or roll it before then"
        ),
    }
