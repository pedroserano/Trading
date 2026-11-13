"""Live orders through Schwab.

Off unless two switches are on: LIVE_TRADING=true in the server's .env file, and the Live
trading switch in Settings. Every live order also passes the same ticket checks, guards and
Schwab preview as a simulated one, then these limits: an allowlist of underlyings (SPY to
start, or any underlying once that box is ticked), a contract cap per leg, and a cap on one
trade's buying power.

Automation on this account proposes at most, unless LIVE_AUTO=true is in .env as well and
Settings has Schwab orders set to Manual and automatic. Then automanage and the scanner may place
live orders themselves, at the levels set for them.

Like the simulator, a new closing order replaces a working closing order on the same
contracts (Schwab's replace call), and an opening order placed with a profit target gets
its GTC closing order once Schwab reports the fill.
"""

import asyncio
import copy
import logging
import time
from datetime import UTC, datetime, timedelta

from .orders import Ticket, TicketLeg, schwab_order

log = logging.getLogger(__name__)
CONFIG_KEY = "live_trading"
STATE_KEY = "live_orders"
DEFAULT_CONFIG = {
    "symbols": ["SPY"],
    "max_contracts": 1,
    "max_bp_share": 0.05,
    "all_symbols": False,  # any underlying, not only the list
}
WORKING = {
    "WORKING",
    "PENDING_ACTIVATION",
    "QUEUED",
    "ACCEPTED",
    "NEW",
    "AWAITING_PARENT_ORDER",
    "AWAITING_CONDITION",
    "AWAITING_STOP_CONDITION",
    "AWAITING_MANUAL_REVIEW",
    "AWAITING_UR_OUT",
    "AWAITING_RELEASE_TIME",
    "PENDING_REPLACE",
    "PENDING_CANCEL",
}
SIGNS = {
    "BUY_TO_OPEN": (1, "open"),
    "SELL_TO_OPEN": (-1, "open"),
    "BUY_TO_CLOSE": (1, "close"),
    "SELL_TO_CLOSE": (-1, "close"),
    "BUY": (1, None),
    "SELL": (-1, None),
    "BUY_TO_COVER": (1, "close"),
    "SELL_SHORT": (-1, "open"),
}


def normalize(order: dict, tracked: dict | None = None) -> dict:
    """A Schwab order in the simulator's shape, so screens and automation read both alike."""
    tracked = tracked or {}
    legs, by_leg = [], {}
    for x in order.get("orderLegCollection") or []:
        sign, effect = SIGNS.get(x.get("instruction"), (1, None))
        q = sign * int(float(x.get("quantity") or 0))
        sym = (x.get("instrument") or {}).get("symbol")
        legs.append({"symbol": sym, "quantity": q, "effect": effect})
        by_leg[x.get("legId")] = q
    effects = {x["effect"] for x in legs}
    intent = tracked.get("intent") or (
        "roll" if effects == {"open", "close"} else "close" if effects == {"close"} else "open"
    )
    price = order.get("price")
    try:
        price = None if price is None else float(price)
    except (TypeError, ValueError):
        price = None
    kind = order.get("orderType")
    if price is None:
        limit = 0.0 if kind == "NET_ZERO" else None
    elif kind == "NET_DEBIT":
        limit = -price
    elif kind == "NET_CREDIT":
        limit = price
    else:
        limit = price if legs and legs[0]["quantity"] < 0 else -price
    units = int(float(order.get("quantity") or 1)) or 1
    # Net per unit, credit positive: what each execution received or paid, over the
    # order's quantity (one unit of a 2-lot vertical is one spread).
    fill = None
    executions = [
        e
        for a in order.get("orderActivityCollection") or []
        if a.get("executionType") == "FILL"
        for e in a.get("executionLegs") or []
    ]
    if executions:
        total = 0.0
        for e in executions:
            sign = 1 if by_leg.get(e.get("legId"), 0) > 0 else -1
            total += -sign * float(e.get("price") or 0) * float(e.get("quantity") or 0)
        fill = round(total / units, 2)
    status = order.get("status") or "UNKNOWN"
    underlying = None
    for x in order.get("orderLegCollection") or []:
        inst = x.get("instrument") or {}
        underlying = underlying or inst.get("underlyingSymbol") or inst.get("symbol")
    return {
        "id": str(order.get("orderId")),
        "status": "WORKING" if status in WORKING else status,
        "schwab_status": status,
        "intent": intent,
        "trade_id": tracked.get("trade_id"),
        "underlying": underlying,
        "strategy": tracked.get("strategy"),
        "legs": legs,
        "units": units,
        "limit_net": limit,
        "duration": "GTC" if order.get("duration") == "GOOD_TILL_CANCEL" else "DAY",
        "created_at": _ts(order.get("enteredTime")),
        "filled_at": _ts(order.get("closeTime")) if status == "FILLED" else None,
        "fill_net": fill if status == "FILLED" else None,
        "placed_by_app": bool(tracked),
        "profit_target": tracked.get("profit_target"),
    }


def _ts(text) -> float | None:
    if not text:
        return None
    text = str(text).replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = text[:-2] + ":" + text[-2:]
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


class LiveBroker:
    POLL_BUSY_S = 10
    POLL_IDLE_S = 60

    def __init__(
        self,
        store,
        schwab,
        source,
        resolve_account,
        server_allowed: bool,
        notifier=None,
        app_url: str = "",
        demo: bool = False,
        server_auto: bool = False,
    ):
        self.store = store
        self.schwab = schwab
        self.source = source
        self.resolve_account = resolve_account
        self.server_allowed = server_allowed and not demo
        self.server_auto = server_auto and self.server_allowed
        self.demo = demo
        self.notifier = notifier
        self.app_url = app_url
        # The automation reads state["orders"], as it does the simulator's.
        self.state: dict = {"orders": []}
        self.refreshed_at: float | None = None
        self.error: str | None = None
        self.lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._mem: dict | None = None

    # Settings ------------------------------------------------------------------------------

    async def config(self) -> dict:
        saved = await self.store.get_setting(CONFIG_KEY) or {}
        return {
            **copy.deepcopy(DEFAULT_CONFIG),
            **{k: v for k, v in saved.items() if k in DEFAULT_CONFIG},
        }

    async def set_config(self, form: dict) -> dict:
        """Saves the Settings form. Raises ValueError with a message for the page."""
        c = await self.config()
        c["all_symbols"] = form.get("all_symbols") in ("on", "true", "1", True)
        if "symbols" in form:
            symbols = [s.strip().upper() for s in str(form["symbols"]).replace(",", " ").split()]
            if not symbols or any(
                not s.replace("$", "").replace(".", "").replace("/", "").isalnum() for s in symbols
            ):
                raise ValueError("Live underlyings: list symbols separated by spaces or commas")
            c["symbols"] = sorted(set(symbols))
        if "max_contracts" in form:
            try:
                n = int(form["max_contracts"])
            except ValueError:
                raise ValueError("Live contracts per leg: not a whole number") from None
            if not 1 <= n <= 50:
                raise ValueError("Live contracts per leg: between 1 and 50")
            c["max_contracts"] = n
        if "max_bp_share" in form:
            try:
                v = float(form["max_bp_share"]) / 100
            except ValueError:
                raise ValueError("Live buying power per trade: not a number") from None
            if not 0.005 <= v <= 0.5:
                raise ValueError("Live buying power per trade: between 0.5% and 50%")
            c["max_bp_share"] = v
        await self.store.set_setting(CONFIG_KEY, c)
        return c

    async def status(self) -> dict:
        c = await self.config()
        return {
            **c,
            "server_allowed": self.server_allowed,
            "demo": self.demo,
            "on": self.server_allowed,
            "server_auto": self.server_auto,
            "auto_cap": self.auto_cap(),
            "refreshed_at": self.refreshed_at,
            "error": self.error,
        }

    def auto_cap(self) -> str:
        """The highest automation level on this account: Auto only with LIVE_TRADING and
        LIVE_AUTO in .env; otherwise Propose."""
        return "auto" if self.server_auto else "propose"

    async def gate(self, t: Ticket, bp_effect: float | None, net_liq: float | None) -> list[str]:
        """Reasons a live order may not go out. Empty when it may."""
        c = await self.config()
        out = []
        if self.demo:
            out.append("Demo mode: there is no real Schwab account to send orders to")
        elif not self.server_allowed:
            out.append("Live trading is not allowed on this server: set LIVE_TRADING=true in .env")
        if not c.get("all_symbols") and t.underlying not in c["symbols"]:
            out.append(
                f"{t.underlying} is not on the live list ({', '.join(c['symbols'])}); "
                "trade it on the simulated account or add it in Settings"
            )
        biggest = max((abs(x.quantity) for x in t.legs if not x.equity), default=0)
        if biggest > c["max_contracts"]:
            out.append(f"{biggest} contracts on one leg; live trading allows {c['max_contracts']}")
        if (
            t.intent in ("open", "adjust")
            and bp_effect is not None
            and net_liq
            and bp_effect > c["max_bp_share"] * net_liq
        ):
            out.append(
                f"Buying power {bp_effect:,.0f} is over {c['max_bp_share']:.1%} of net liq, "
                "the live limit for one trade"
            )
        return out

    # State ---------------------------------------------------------------------------------

    async def _load(self) -> dict:
        if self._mem is None:
            self._mem = await self.store.get_setting(STATE_KEY) or {}
        self._mem.setdefault("tracked", {})
        self._mem.setdefault("audit", [])
        return self._mem

    async def _save(self) -> None:
        self._mem["audit"] = self._mem["audit"][-2000:]
        cutoff = time.time() - 30 * 86400
        self._mem["tracked"] = {
            k: v for k, v in self._mem["tracked"].items() if v.get("placed_at", 0) > cutoff
        }
        await self.store.set_setting(STATE_KEY, self._mem)

    def _audit(self, action: str, **fields) -> None:
        self._mem["audit"].append({"at": time.time(), "action": action, **fields})
        log.info("Live order %s: %s", action, {k: v for k, v in fields.items() if k != "body"})

    async def audit(self) -> list[dict]:
        return list(reversed((await self._load())["audit"][-200:]))

    # Orders --------------------------------------------------------------------------------

    async def refresh(self) -> list[dict]:
        """Reads the last week's orders from Schwab, and acts on new fills."""
        mem = await self._load()
        account = await self.resolve_account()
        now = datetime.now(UTC)
        raw = await self.source.orders(account, now - timedelta(days=7), now)
        orders = [normalize(o, mem["tracked"].get(str(o.get("orderId")))) for o in raw or []]
        self.state = {"orders": orders}
        self.refreshed_at = time.time()
        self.error = None
        await self._after_fills(orders)
        return orders

    async def working(self) -> list[dict]:
        return [o for o in self.state["orders"] if o["status"] == "WORKING"]

    async def place(
        self,
        t: Ticket,
        trade_id=None,
        profit_target: float | None = None,
        entry: dict | None = None,
    ) -> dict:
        """Sends the order to Schwab. The caller has already checked guards and gates."""
        async with self.lock:
            mem = await self._load()
            account = await self.resolve_account()
            body = schwab_order(t)
            replaced = None
            if t.intent in ("close", "roll"):
                await self.refresh()
                symbols = {x.symbol for x in t.legs}
                old = [
                    o
                    for o in await self.working()
                    if o["intent"] in ("close", "roll")
                    and symbols & {leg["symbol"] for leg in o["legs"]}
                ]
                for extra in old[1:]:
                    await self.schwab.cancel_order(account, extra["id"])
                    self._audit("cancel", order_id=extra["id"], reason="replaced")
                if old:
                    replaced = old[0]["id"]
            if replaced:
                order_id = await self.schwab.replace_order(account, replaced, body)
                self._audit("replace", order_id=order_id, replaces=replaced, body=body)
            else:
                order_id = await self.schwab.place_order(account, body)
                self._audit("place", order_id=order_id, body=body)
            mem["tracked"][order_id] = {
                "trade_id": trade_id,
                "intent": t.intent,
                "profit_target": profit_target if t.intent == "open" else None,
                "placed_at": time.time(),
                "entry": entry,
                "target_placed": False,
            }
            await self._save()
            try:
                await self.refresh()  # so the new order shows (and counts) at once
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
            self._wake.set()
            return {
                "id": order_id,
                "status": "WORKING",
                "intent": t.intent,
                "trade_id": trade_id,
                "limit_net": t.limit_net,
                "duration": t.duration,
                "replaces": [replaced] if replaced else [],
                "legs": [
                    {"symbol": x.symbol, "quantity": x.quantity, "effect": x.effect} for x in t.legs
                ],
            }

    async def cancel(self, order_id) -> dict:
        async with self.lock:
            await self._load()
            account = await self.resolve_account()
            await self.schwab.cancel_order(account, str(order_id))
            self._audit("cancel", order_id=str(order_id))
            await self._save()
            self._wake.set()
            return {"id": str(order_id), "status": "CANCELED"}

    async def _after_fills(self, orders: list[dict]) -> None:
        mem = self._mem
        changed = False
        for o in orders:
            info = mem["tracked"].get(o["id"])
            if not info or o["status"] != "FILLED" or info.get("fill_seen"):
                continue
            info["fill_seen"] = True
            changed = True
            self._audit("filled", order_id=o["id"], fill_net=o["fill_net"])
            await self._notify(
                "Order filled",
                f"#{o['id']} {o['underlying']} {o['intent']}"
                + (
                    f" at {abs(o['fill_net']):.2f} {'credit' if o['fill_net'] > 0 else 'debit'}"
                    if o["fill_net"] is not None
                    else ""
                ),
            )
            pt = info.get("profit_target")
            if pt and o["intent"] == "open" and o["fill_net"] and o["fill_net"] > 0:
                await self._place_target(o, pt)
        if changed:
            await self._save()

    async def _place_target(self, o: dict, pt: float) -> None:
        """The GTC closing order at the profit target, once the opening order has filled."""
        limit = -round(o["fill_net"] * (1 - pt), 2)
        try:
            t = Ticket(
                legs=[TicketLeg.parse(x["symbol"], -x["quantity"], "close") for x in o["legs"]],
                intent="close",
                duration="GTC",
                limit_net=limit,
                trade_id=f"c-{o['id']}",
            )
            if not (await self.status())["on"]:
                raise ValueError("live trading was switched off before the fill")
            account = await self.resolve_account()
            order_id = await self.schwab.place_order(account, schwab_order(t))
        except Exception as e:
            self._audit("target-failed", order_id=o["id"], error=str(e))
            await self._notify("Profit target not placed", f"#{o['id']}: {e}")
            return
        self._mem["tracked"][order_id] = {
            "trade_id": f"c-{o['id']}",
            "intent": "close",
            "profit_target": None,
            "placed_at": time.time(),
            "target_of": o["id"],
        }
        self._mem["tracked"][o["id"]]["target_placed"] = order_id
        self._audit("target", order_id=order_id, for_order=o["id"], limit=limit)
        await self._notify(
            "Profit target placed",
            f"GTC close at {abs(limit):.2f} debit ({round(pt * 100)}% of the credit), "
            f"order #{order_id}",
        )

    async def _notify(self, title: str, message: str) -> None:
        if self.notifier is not None and self.notifier.enabled:
            try:
                await self.notifier.send(f"[Live] {title}", message, self.app_url)
            except Exception as e:
                log.warning("Live notification failed: %s", e)

    def refresh_soon(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while True:
            self._wake.clear()
            busy = False
            if self.server_allowed and not self.demo:
                try:
                    async with self.lock:
                        orders = await self.refresh()
                    busy = any(o["status"] == "WORKING" and o["placed_by_app"] for o in orders)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.error = f"{type(e).__name__}: {e}"
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.POLL_BUSY_S if busy else self.POLL_IDLE_S
                )
            except TimeoutError:
                pass
