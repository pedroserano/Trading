"""Order check: compares orders placed in thinkorswim with what the app would build.

Schwab's API returns every order on the account in the same JSON the app sends, whatever
placed it. For each order this module rebuilds the app's version from the same legs, price
and duration, and records what differs. Placing an order in thinkorswim at a price that
cannot fill, then cancelling it, is enough to check one shape.

Read-only: it never places, changes or cancels anything.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from .orders import Ticket, TicketError, TicketLeg, schwab_order, validate_shape
from .schwab_service import SchwabError, SchwabNotConnected

log = logging.getLogger(__name__)
STORE_KEY = "order_checks"
LOOKBACK_DAYS = 30
INTERVAL_S = 300
KEEP = 300

# The fields the app sends; these must agree.
COMPARED = ("orderType", "session", "duration", "orderStrategyType", "complexOrderStrategyType")
# Fields Schwab adds to its record of an order; not part of what was sent.
RESPONSE_ONLY = {
    "orderId",
    "status",
    "enteredTime",
    "closeTime",
    "accountNumber",
    "cancelable",
    "editable",
    "tag",
    "filledQuantity",
    "remainingQuantity",
    "statusDescription",
    "orderActivityCollection",
    "requestedDestination",
    "destinationLinkName",
    "releaseTime",
    "cancelTime",
    "orderLegCollection",
    "price",
    "quantity",
    "childOrderStrategies",
    "replacingOrderCollection",
    "replacedOrderCollection",
} | set(COMPARED)
EFFECTS = {
    "BUY_TO_OPEN": (1, "open"),
    "SELL_TO_OPEN": (-1, "open"),
    "BUY_TO_CLOSE": (1, "close"),
    "SELL_TO_CLOSE": (-1, "close"),
}


def ticket_from_schwab(order: dict) -> tuple[Ticket | None, str | None]:
    """The app's ticket for the same order, or (None, why it cannot be compared)."""
    if order.get("orderStrategyType") != "SINGLE":
        return None, (
            f"{order.get('orderStrategyType')} order (conditional orders such as OCO or "
            "first-triggers are not built by the app)"
        )
    if order.get("orderType") not in ("LIMIT", "NET_CREDIT", "NET_DEBIT", "NET_ZERO"):
        return None, f"{order.get('orderType')} order (the app only sends limit orders)"
    legs = []
    for x in order.get("orderLegCollection") or []:
        inst = x.get("instrument") or {}
        if inst.get("assetType") != "OPTION":
            return None, f"has a {inst.get('assetType', 'non-option').lower()} leg"
        sign_effect = EFFECTS.get(x.get("instruction"))
        if sign_effect is None:
            return None, f"instruction {x.get('instruction')} is not an option instruction"
        sign, effect = sign_effect
        try:
            legs.append(TicketLeg.parse(inst["symbol"], sign * int(x["quantity"]), effect))
        except (KeyError, ValueError, TicketError) as e:
            return None, f"leg could not be read ({e})"
    if not legs:
        return None, "no legs"
    effects = {leg.effect for leg in legs}
    intent = "roll" if effects == {"open", "close"} else effects.pop()
    price = order.get("price")
    try:
        price = None if price is None else abs(float(price))
    except (TypeError, ValueError):
        return None, "price could not be read"
    if order["orderType"] == "NET_ZERO":
        limit = 0.0
    elif price is None:
        return None, "no limit price"
    elif order["orderType"] == "NET_DEBIT":
        limit = -price
    elif order["orderType"] == "NET_CREDIT":
        limit = price
    else:  # single leg: selling takes in a credit
        limit = price if legs[0].quantity < 0 else -price
    duration = "GTC" if order.get("duration") == "GOOD_TILL_CANCEL" else "DAY"
    t = Ticket(legs=legs, intent=intent, duration=duration, limit_net=limit)
    try:
        validate_shape(t)
    except TicketError as e:
        return None, str(e)
    if order.get("duration") not in ("DAY", "GOOD_TILL_CANCEL"):
        t.warnings.append(f"duration {order.get('duration')}")
    return t, None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compare(theirs: dict, ours: dict) -> list[dict]:
    """Differences between Schwab's record of an order and the app's body for it.

    level "differs": the app would send something else. "note": worth knowing, not wrong.
    """
    out = []

    def add(field, schwab, app, level="differs", note=None):
        out.append({"field": field, "schwab": schwab, "app": app, "level": level, "note": note})

    for k in COMPARED:
        if theirs.get(k) != ours.get(k):
            add(k, theirs.get(k), ours.get(k))
    tp, op = _num(theirs.get("price")), _num(ours.get("price"))
    if (tp is None) != (op is None) or (tp is not None and abs(tp - op) > 0.0001):
        add("price", theirs.get("price"), ours.get("price"))
    tq, oq = theirs.get("quantity"), ours.get("quantity")
    if oq is None:
        leg_qty = ours["orderLegCollection"][0]["quantity"]
        if tq is not None and _num(tq) != leg_qty:
            add("quantity", tq, None)
        elif tq is not None:
            add(
                "quantity",
                tq,
                None,
                "note",
                "Schwab records an order quantity; the app sends a single leg's quantity on "
                "the leg only, as in Schwab's examples",
            )
    elif _num(tq) != oq:
        add("quantity", tq, oq)
    t_legs = {
        (x.get("instrument") or {}).get("symbol"): x for x in theirs.get("orderLegCollection") or []
    }
    o_legs = {x["instrument"]["symbol"]: x for x in ours["orderLegCollection"]}
    for symbol in sorted(set(t_legs) | set(o_legs)):
        t, o = t_legs.get(symbol), o_legs.get(symbol)
        if t is None or o is None:
            add(f"leg {symbol}", "present" if t else "missing", "present" if o else "missing")
            continue
        if t.get("instruction") != o["instruction"]:
            add(f"leg {symbol} instruction", t.get("instruction"), o["instruction"])
        if _num(t.get("quantity")) != o["quantity"]:
            add(f"leg {symbol} quantity", t.get("quantity"), o["quantity"])
    extra = sorted(k for k, v in theirs.items() if k not in RESPONSE_ONLY and v not in (None, ""))
    for k in extra:
        add(k, theirs[k], None, "note", "Sent by thinkorswim; the app leaves it out")
    return out


def check(order: dict) -> dict:
    """One log entry for one Schwab order."""
    legs = order.get("orderLegCollection") or []
    under = None
    for x in legs:
        inst = x.get("instrument") or {}
        under = under or inst.get("underlyingSymbol") or (inst.get("symbol") or "")[:6].strip()
    entry = {
        "order_id": order.get("orderId"),
        "entered_at": order.get("enteredTime"),
        "status": order.get("status"),
        "underlying": under,
        "legs": [
            f"{x.get('instruction', '?')} {x.get('quantity', '?')} "
            f"{(x.get('instrument') or {}).get('symbol', '?')}"
            for x in legs
        ],
        "schwab": order,
        "app": None,
        "diffs": [],
        "reason": None,
    }
    t, reason = ticket_from_schwab(order)
    if t is None:
        entry["result"] = "not comparable"
        entry["reason"] = reason
        return entry
    try:
        ours = schwab_order(t)
    except TicketError as e:
        entry.update(result="not comparable", reason=str(e))
        return entry
    entry["app"] = ours
    entry["diffs"] = compare(order, ours)
    entry["result"] = "differs" if any(d["level"] == "differs" for d in entry["diffs"]) else "match"
    return entry


class OrderCheckService:
    def __init__(self, source, store, resolve_account, enabled=lambda: True):
        self.source = source
        self.store = store
        self.resolve_account = resolve_account
        self.enabled = enabled
        self.checked_at: float | None = None
        self.error: str | None = None
        self._wake = asyncio.Event()

    def refresh_soon(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while True:
            self._wake.clear()
            if self.enabled():
                try:
                    await self.sync()
                except asyncio.CancelledError:
                    raise
                except (SchwabNotConnected, SchwabError) as e:
                    self.error = str(e)
                except Exception as e:
                    log.exception("Order check failed")
                    self.error = f"{type(e).__name__}: {e}"
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=INTERVAL_S)
                await asyncio.sleep(5)  # let Schwab record the order first
            except TimeoutError:
                pass

    async def sync(self) -> int:
        """Reads the last month's orders and re-checks every one. Returns how many."""
        account_hash = await self.resolve_account()
        now = datetime.now(UTC)
        orders = await self.source.orders(account_hash, now - timedelta(days=LOOKBACK_DAYS), now)
        saved = await self.store.get_setting(STORE_KEY) or {}
        for order in orders or []:
            if order.get("orderId") is None:
                continue
            entry = check(order)
            old = saved.get(str(order["orderId"]))
            entry["first_seen"] = (old or {}).get("first_seen") or time.time()
            saved[str(order["orderId"])] = entry
        # Keep the newest, so orders older than the look-back stay in the log for a while.
        kept = sorted(saved.values(), key=lambda e: e.get("entered_at") or "", reverse=True)[:KEEP]
        await self.store.set_setting(STORE_KEY, {str(e["order_id"]): e for e in kept})
        self.checked_at = time.time()
        self.error = None
        return len(orders or [])

    async def view(self) -> dict:
        saved = await self.store.get_setting(STORE_KEY) or {}
        entries = sorted(saved.values(), key=lambda e: e.get("entered_at") or "", reverse=True)
        counts: dict[str, int] = {}
        for e in entries:
            counts[e["result"]] = counts.get(e["result"], 0) + 1
        return {
            "entries": entries,
            "counts": counts,
            "checked_at": self.checked_at,
            "error": self.error,
        }
