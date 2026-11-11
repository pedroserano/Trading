"""Trade chains for the Schwab account, rebuilt from its transaction history.

A chain is one position's life: the opening order, every roll and adjustment, and the close,
expiration or assignment. The simulated account records chains as it trades; the Schwab
account's are reconstructed here from transactions, so trades placed in thinkorswim are
covered too.

Rules, applied in time order:
- An order that only opens starts a new chain.
- Closing legs reduce whichever open chain holds them (oldest first).
- An order that closes legs of one chain and opens new legs on the same underlying is a roll
  of that chain.
- An order that only opens, placed within LINK_WINDOW_S of a full close of a chain on the same
  underlying with the same shape, is linked as a roll of that chain (thinkorswim users often
  roll as two orders). Such links are marked so they can be split.
- Expirations, assignments and exercises close legs; shares received by assignment join the
  assigned option's chain.
- Positions held before the start of the history become "carried in" chains, priced at the
  position's average cost.
- Manual overrides (link a group to a chain, or split a group into its own chain) win.

Schwab's transaction format is only partly documented, so parsing is defensive and the raw
transactions are kept.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .options import Leg, parse_option_symbol
from .strategies import group

LINK_WINDOW_S = 15 * 60
LEG_TYPES = {"OPTION", "EQUITY", "COLLECTIVE_INVESTMENT", "ETF"}


@dataclass
class Fill:
    symbol: str
    underlying: str
    quantity: float  # signed: + bought, - sold
    price: float
    effect: str | None  # "open", "close", or None when Schwab did not say
    multiplier: int
    put_call: str | None = None


@dataclass
class Group:
    key: str  # order id, or the activity id when there is no order
    time: float
    kind: str  # trade, expire, assign, exercise
    fills: list[Fill]
    fees: float  # positive = paid
    description: str = ""


@dataclass
class Chain:
    id: str
    underlying: str
    strategy: str
    opened_at: float
    events: list[dict] = field(default_factory=list)
    holdings: dict[str, float] = field(default_factory=dict)
    carried_in: bool = False
    closed_at: float | None = None

    @property
    def open(self) -> bool:
        return any(abs(q) > 1e-9 for q in self.holdings.values())


def _ts(value) -> float:
    if isinstance(value, int | float):
        return float(value) / (1000 if value > 1e11 else 1)
    text = str(value).replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = text[:-2] + ":" + text[-2:]  # +0000 -> +00:00
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def normalize(raw: dict) -> Group | None:
    """One Schwab transaction as a group of fills. None for anything that is not a trade."""
    typ = raw.get("type")
    if typ not in ("TRADE", "RECEIVE_AND_DELIVER"):
        return None
    desc = str(raw.get("description") or "")
    low = desc.lower()
    kind = "trade"
    if typ == "RECEIVE_AND_DELIVER":
        kind = "assign" if "assign" in low else "exercise" if "exercis" in low else "expire"
    fills, leg_cash = [], 0.0
    for item in raw.get("transferItems") or []:
        inst = item.get("instrument") or {}
        asset = inst.get("assetType")
        if asset not in LEG_TYPES or item.get("feeType"):
            continue
        symbol = inst.get("symbol") or ""
        amount = float(item.get("amount") or 0)
        cost = float(item.get("cost") or 0)
        price = float(item.get("price") or 0)
        if amount == 0:
            continue
        # Cost is negative for a purchase and positive for a sale.
        qty = -abs(amount) if cost > 0 else abs(amount) if cost < 0 else amount
        occ = parse_option_symbol(symbol) if asset == "OPTION" else None
        mult = 100 if asset == "OPTION" else 1
        effect = {"OPENING": "open", "CLOSING": "close"}.get(item.get("positionEffect"))
        fills.append(
            Fill(
                symbol=symbol,
                underlying=inst.get("underlyingSymbol") or (occ.underlying if occ else symbol),
                quantity=qty,
                price=price,
                effect=effect,
                multiplier=mult,
                put_call=occ.put_call if occ else None,
            )
        )
        leg_cash += cost if cost else -qty * price * mult
    if not fills:
        return None
    net = raw.get("netAmount")
    fees = round(leg_cash - float(net), 2) if net is not None else 0.0
    key = str(raw.get("orderId") or f"a{raw.get('activityId')}")
    return Group(
        key=key,
        time=_ts(raw.get("time") or raw.get("tradeDate")),
        kind=kind,
        fills=fills,
        fees=max(fees, 0.0),
        description=desc,
    )


def merge(groups: list[Group]) -> list[Group]:
    """Combines transactions that belong to one order (partial fills, one per leg)."""
    by_key: dict[str, Group] = {}
    for g in sorted(groups, key=lambda g: g.time):
        if g.key in by_key and g.kind == by_key[g.key].kind:
            m = by_key[g.key]
            m.fills.extend(g.fills)
            m.fees = round(m.fees + g.fees, 2)
        else:
            by_key[g.key if g.key not in by_key else f"{g.key}:{g.time}"] = g
    return sorted(by_key.values(), key=lambda g: g.time)


def _shape(fills: list[Fill]) -> tuple:
    return tuple(sorted((f.put_call or "S", round(abs(f.quantity))) for f in fills))


def _name(fills: list[Fill], opening: bool = True) -> str:
    legs = []
    for f in fills:
        occ = parse_option_symbol(f.symbol)
        legs.append(
            Leg(
                symbol=f.symbol,
                underlying=f.underlying,
                kind="OPTION" if occ else "EQUITY",
                quantity=f.quantity if opening else -f.quantity,
                avg_price=0.0,
                put_call=occ.put_call if occ else None,
                strike=occ.strike if occ else None,
                expiration=occ.expiration if occ else None,
                multiplier=f.multiplier,
            )
        )
    found = group(legs)
    return found[0].name if len(found) == 1 else "Custom"


def build(
    groups: list[Group],
    positions: dict[str, dict],
    overrides: dict | None = None,
    history_start: float | None = None,
) -> dict:
    """Chains from transaction groups.

    positions: current holdings, {symbol: {"quantity", "avg_price", "underlying", "multiplier"}}.
    overrides: {"link": {group_key: chain_id}, "split": [group_key, ...]}.
    """
    overrides = overrides or {}
    links: dict[str, str] = overrides.get("link", {})
    splits = set(overrides.get("split", []))
    groups = sorted(groups, key=lambda g: g.time)
    start = history_start if history_start is not None else (groups[0].time - 1 if groups else 0.0)

    chains: dict[str, Chain] = {}
    order: list[str] = []

    def new_chain(cid, underlying, strategy, t, carried=False) -> Chain:
        c = Chain(id=cid, underlying=underlying, strategy=strategy, opened_at=t, carried_in=carried)
        chains[cid] = c
        order.append(cid)
        return c

    # Holdings before the history begins: what is held now, less what the history adds.
    # This covers positions still held and positions the history shows being closed.
    window = defaultdict(float)
    seen: dict[str, Fill] = {}
    for g in groups:
        for f in g.fills:
            window[f.symbol] += f.quantity
            seen.setdefault(f.symbol, f)
    carried: dict[str, list[Fill]] = defaultdict(list)
    for sym in sorted(set(positions) | set(window)):
        pos = positions.get(sym, {})
        before = pos.get("quantity", 0.0) - window.get(sym, 0.0)
        if abs(before) <= 1e-9:
            continue
        occ = parse_option_symbol(sym)
        known = seen.get(sym)
        under = (
            pos.get("underlying")
            or (known.underlying if known else None)
            or (occ.underlying if occ else sym)
        )
        carried[under].append(
            Fill(
                symbol=sym,
                underlying=under,
                quantity=before,
                # Average cost when Schwab reports the position; otherwise the price of the
                # first trade in the history that touched it.
                price=pos.get("avg_price") or (known.price if known else 0.0),
                effect="open",
                multiplier=pos.get("multiplier") or (100 if occ else 1),
                put_call=occ.put_call if occ else None,
            )
        )
    for under, fills in sorted(carried.items()):
        legs = [
            Leg(
                symbol=f.symbol,
                underlying=under,
                kind="OPTION" if f.put_call else "EQUITY",
                quantity=f.quantity,
                avg_price=f.price,
                put_call=f.put_call,
                strike=(parse_option_symbol(f.symbol).strike if f.put_call else None),
                expiration=(parse_option_symbol(f.symbol).expiration if f.put_call else None),
                multiplier=f.multiplier,
            )
            for f in fills
        ]
        for i, st in enumerate(group(legs)):
            c = new_chain(f"c-{under}-{i}", under, st.name, start, carried=True)
            ev_fills = [f for f in fills if any(x.symbol == f.symbol for x in st.legs)]
            for leg in st.legs:
                c.holdings[leg.symbol] = c.holdings.get(leg.symbol, 0.0) + leg.quantity
            c.events.append(
                _event(
                    "carried",
                    start,
                    None,
                    [
                        (f, next(x.quantity for x in st.legs if x.symbol == f.symbol))
                        for f in ev_fills
                    ],
                    0.0,
                    note=(
                        "Held before the transaction history starts; priced at average cost"
                        if all(f.symbol in positions for f in ev_fills)
                        else "Opened before the transaction history starts; the opening price "
                        "is unknown, so this chain's P/L is incomplete"
                    ),
                )
            )

    recent_closes: list[tuple[float, str, str, tuple]] = []  # time, underlying, chain, shape
    proposals: list[dict] = []
    unmatched: list[dict] = []

    for g in groups:
        closes, opens = [], []
        for f in g.fills:
            holders = [
                cid for cid in order if chains[cid].holdings.get(f.symbol, 0) * f.quantity < 0
            ]
            is_close = (
                f.effect == "close"
                or (f.effect is None and holders)
                or (g.kind in ("expire", "assign", "exercise") and f.put_call)
            )
            (closes if is_close and holders else opens).append(f)
            if is_close and not holders:
                unmatched.append({"group": g.key, "symbol": f.symbol, "quantity": f.quantity})

        # Allocate closing quantities to chains, oldest first.
        touched: dict[str, list[tuple[Fill, float]]] = defaultdict(list)
        for f in closes:
            left = f.quantity
            for cid in order:
                held = chains[cid].holdings.get(f.symbol, 0.0)
                if held * left >= 0 or abs(left) < 1e-9:
                    continue
                take = -held if abs(held) <= abs(left) else left
                chains[cid].holdings[f.symbol] = held + take
                touched[cid].append((f, take))
                left -= take
            if abs(left) > 1e-9:
                unmatched.append({"group": g.key, "symbol": f.symbol, "quantity": left})

        target = None
        auto_linked = False
        if opens:
            unders = {f.underlying for f in opens}
            if g.key in links and links[g.key] in chains:
                target = links[g.key]
            elif g.key not in splits:
                same = [cid for cid in touched if chains[cid].underlying in unders]
                if same:
                    target = max(same, key=lambda c: len(touched[c]))
                elif g.kind == "assign" or any(f.put_call is None for f in opens):
                    # Shares from an assignment join the assigned option's chain.
                    for t_, under, cid, _ in reversed(recent_closes):
                        if (
                            under in unders
                            and g.time - t_ < 3 * 86400
                            and any(e["kind"] in ("assign", "exercise") for e in chains[cid].events)
                        ):
                            target = cid
                            break
                if target is None:
                    shape = _shape(opens)
                    for t_, under, cid, sh in reversed(recent_closes):
                        if under in unders and g.time - t_ <= LINK_WINDOW_S and sh == shape:
                            target, auto_linked = cid, True
                            break
                        if under in unders and g.time - t_ <= 4 * 3600:
                            proposals.append(
                                {
                                    "group": g.key,
                                    "chain": cid,
                                    "reason": "Opened soon after this chain closed",
                                }
                            )
                            break
            if target is None:
                target = new_chain(f"c-{g.key}", opens[0].underlying, _name(opens), g.time).id

        # Events: each touched chain gets its closes; the target gets the opens too.
        fee_total = g.fees
        contracts = sum(abs(f.quantity) for f in g.fills) or 1
        involved = list(touched) + ([target] if target and target not in touched else [])
        for cid in involved:
            c = chains[cid]
            legs = [(f, q) for f, q in touched.get(cid, [])]
            if cid == target:
                legs += [(f, f.quantity) for f in opens]
                for f in opens:
                    c.holdings[f.symbol] = c.holdings.get(f.symbol, 0.0) + f.quantity
            n = sum(abs(q) for _, q in legs)
            fee = round(fee_total * n / contracts, 2)
            if g.kind != "trade":
                kind = g.kind
            elif cid == target and touched.get(cid):
                kind = "roll"
            elif cid == target and len(c.events) > 0:
                kind = "roll" if auto_linked else "adjust"
            elif cid == target:
                kind = "open"
            else:
                kind = "close"
            note = None
            if auto_linked and cid == target:
                note = "Linked as a roll: opened within 15 minutes of closing this chain"
            elif cid == target and links.get(g.key) == cid:
                note = "Linked by you"
            c.events.append(
                _event(
                    kind,
                    g.time,
                    g.key,
                    legs,
                    fee,
                    note=note,
                    auto_linked=auto_linked and cid == target,
                )
            )
            if not c.open:
                c.closed_at = g.time
                recent_closes.append((g.time, c.underlying, cid, _shape([f for f, _ in legs])))
            else:
                c.closed_at = None

    # Reconcile with what Schwab says is held now.
    held = defaultdict(float)
    for c in chains.values():
        for sym, q in c.holdings.items():
            held[sym] += q
    mismatches = []
    for sym in set(held) | set(positions):
        want = positions.get(sym, {}).get("quantity", 0.0)
        if abs(held.get(sym, 0.0) - want) > 1e-6:
            mismatches.append({"symbol": sym, "chains": held.get(sym, 0.0), "schwab": want})

    return {
        "chains": [_chain_json(chains[cid]) for cid in order],
        "proposals": [p for p in proposals if p["chain"] in chains],
        "unmatched": unmatched,
        "mismatches": sorted(mismatches, key=lambda m: m["symbol"]),
    }


def _event(kind, t, key, legs, fee, note=None, auto_linked=False) -> dict:
    net = round(sum(-q * f.price * f.multiplier for f, q in legs), 2)
    return {
        "at": t,
        "kind": kind,
        "group": key,
        "order_id": key,
        "net": net,
        "commission": fee,
        "legs": [{"symbol": f.symbol, "quantity": q, "price": f.price} for f, q in legs],
        "note": note,
        "auto_linked": auto_linked,
    }


def _chain_json(c: Chain) -> dict:
    status = "open" if c.open else "closed"
    realized = None
    if status == "closed":
        realized = round(sum(e["net"] - e["commission"] for e in c.events), 2)
    return {
        "id": c.id,
        "underlying": c.underlying,
        "strategy": c.strategy,
        "opened_at": c.opened_at,
        "closed_at": c.closed_at if status == "closed" else None,
        "status": status,
        "carried_in": c.carried_in,
        "events": c.events,
        "realized_pl": realized,
        "holdings": {s: q for s, q in c.holdings.items() if abs(q) > 1e-9},
    }


def trade_info(chains: list[dict]) -> dict:
    """Per open chain, the same summary the simulated broker gives the portfolio."""
    return {
        c["id"]: {
            "strategy": c["strategy"],
            "net": round(sum(e["net"] for e in c["events"]), 2),
            "rolls": sum(1 for e in c["events"] if e["kind"] == "roll"),
            "opened_at": c["opened_at"],
        }
        for c in chains
        if c["status"] == "open"
    }
