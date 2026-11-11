"""Import of the "Options P/L" trading-history spreadsheet, and reconciliation with Schwab.

The Trades tab has one row per leg: the opening in columns A-P, the account in Q, the status
in R, and the closing in S-Y. Spreads are sometimes entered as one row with all strikes
("2800/2750") and a net price. See PLAN.md for the reasoning.
"""

import hashlib
import io
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from .chains import Fill, Group
from .options import option_symbol

ET = ZoneInfo("America/New_York")

# Column positions on the Trades tab (0-based).
COL = {
    "underlying": 0,
    "strikes": 1,
    "strat": 2,
    "exp": 3,
    "open_date": 5,
    "stock_price": 6,
    "legs": 7,
    "shares": 8,
    "open_price": 9,
    "open_commission": 10,
    "bpr": 15,
    "account": 16,
    "status": 17,
    "close_date": 18,
    "close_shares": 20,
    "close_price": 21,
    "close_commission": 22,
    "pl": 24,
}
SINGLE = {"P": ("P", -1), "C": ("C", -1), "P+": ("P", 1), "C+": ("C", 1)}
STOCK_CODES = {None, "", "+100", "+1000"}
MATCH_DAYS = 3  # sheet dates can be a day or two off, and weekends sit between


class SheetError(ValueError):
    pass


@dataclass
class SheetRow:
    key: str
    account: str
    data: dict  # cleaned cell values, JSON-safe


@dataclass
class Converted:
    groups: list[Group] = field(default_factory=list)
    futures: list[dict] = field(default_factory=list)  # history-only records
    # The sheet's own P/L for each finished row: [{"time", "amount", "underlying", "row"}].
    realized: list[dict] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)  # rows that could not be used


# Reading ------------------------------------------------------------------------------


def _clean(v):
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def read_workbook(content: bytes, account: str = "Joint") -> tuple[list[SheetRow], dict]:
    """Rows for one account from an uploaded .xlsx file, keyed by their opening details."""
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:
        raise SheetError(f"That file could not be read as an Excel workbook ({e})") from e
    if "Trades" not in wb.sheetnames:
        raise SheetError("The workbook has no Trades tab")
    ws = wb["Trades"]
    header = [str(c or "").strip() for c in next(ws.iter_rows(max_row=1, values_only=True))]
    if header[:4] != ["R", "Strikes", "Strat", "Exp"] or header[16:18] != ["Account", "Status"]:
        raise SheetError(
            "The Trades tab's columns are not laid out as expected "
            f"(found {header[:4]} ... {header[16:18]})"
        )
    rows, counts, seen = [], {"rows": 0, "other_accounts": 0}, {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or not r[0]:
            continue
        counts["rows"] += 1
        data = {name: _clean(r[i]) if i < len(r) else None for name, i in COL.items()}
        if (data["account"] or "") != account:
            counts["other_accounts"] += 1
            continue
        # The opening details identify a row; an occurrence number separates identical rows.
        ident = "|".join(
            str(data[k])
            for k in ("underlying", "strikes", "strat", "exp", "open_date", "shares", "open_price")
        )
        n = seen.get(ident, 0)
        seen[ident] = n + 1
        key = hashlib.sha256(f"{ident}|{n}".encode()).hexdigest()[:16]
        rows.append(SheetRow(key=key, account=account, data=data))
    counts["account_rows"] = len(rows)
    return rows, counts


# Converting ---------------------------------------------------------------------------


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _day(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _ts(d: date, hh: int, mm: int, ss: int = 0) -> float:
    return datetime.combine(d, time(hh, mm, ss), ET).timestamp()


def _legs(row: dict) -> tuple[list[tuple[str, int, float, str | None]], str | None]:
    """[(symbol, signed quantity, price share, put/call)], or a reason it cannot be used.

    Price share: the part of the row's price each leg carries. Spreads entered as one row
    only have a net price, so it is all put on the first short leg; the net is right even
    though the split between legs is not known.
    """
    under = str(row["underlying"]).strip().upper()
    strat, strikes, exp = row["strat"], row["strikes"], _day(row["exp"])
    shares = _num(row["shares"])
    if shares is None or shares <= 0:
        return [], "no quantity"
    qty = shares / 100
    if strat in SINGLE:
        k = _num(strikes)
        if k is None or exp is None:
            return [], "missing strike or expiration"
        pc, side = SINGLE[strat]
        return [(option_symbol(under, exp, pc, k), side * qty, 1.0, pc)], None
    parts = [_num(x) for x in str(strikes or "").split("/")]
    if exp is None or not parts or any(p is None for p in parts):
        return [], f"unrecognised spread strikes {strikes!r}"
    ks = sorted(parts)
    if strat == "Vert P" and len(ks) == 2:
        return [
            (option_symbol(under, exp, "P", ks[1]), -qty, 1.0, "P"),
            (option_symbol(under, exp, "P", ks[0]), qty, 0.0, "P"),
        ], None
    if strat in ("Vert C", "CCS") and len(ks) == 2:
        return [
            (option_symbol(under, exp, "C", ks[0]), -qty, 1.0, "C"),
            (option_symbol(under, exp, "C", ks[1]), qty, 0.0, "C"),
        ], None
    if strat == "STR" and len(ks) == 2:
        return [
            (option_symbol(under, exp, "P", ks[0]), -qty, 1.0, "P"),
            (option_symbol(under, exp, "C", ks[1]), -qty, 0.0, "C"),
        ], None
    if strat == "STR" and len(ks) == 1:
        return [
            (option_symbol(under, exp, "P", ks[0]), -qty, 1.0, "P"),
            (option_symbol(under, exp, "C", ks[0]), -qty, 0.0, "C"),
        ], None
    if strat == "IC" and len(ks) == 4:
        return [
            (option_symbol(under, exp, "P", ks[1]), -qty, 1.0, "P"),
            (option_symbol(under, exp, "P", ks[0]), qty, 0.0, "P"),
            (option_symbol(under, exp, "C", ks[2]), -qty, 0.0, "C"),
            (option_symbol(under, exp, "C", ks[3]), qty, 0.0, "C"),
        ], None
    return [], f"strategy {strat!r} with strikes {strikes!r} is not recognised"


def convert(rows: list[SheetRow]) -> Converted:
    out = Converted()
    opens: dict[str, Group] = {}
    closes: list[tuple[SheetRow, Group, str]] = []  # row, group, status
    for row in rows:
        d = row.data
        under = str(d["underlying"]).strip().upper()
        status = (d["status"] or "").strip()
        opened = _day(d["open_date"])
        if under.startswith("/"):
            out.futures.append(_futures(row))
            continue
        pl = _num(d["pl"])
        if status in ("Closed", "Rolled", "Expired", "Assigned") and pl is not None:
            when = _day(d["close_date"]) or _day(d["exp"]) or opened
            if when:
                out.realized.append(
                    {"time": _ts(when, 16, 0), "amount": pl, "underlying": under, "row": row.key}
                )
        cs, os_ = _num(d.get("close_shares")), _num(d["shares"])
        if cs is not None and os_ is not None and abs(cs - os_) > 1e-9 and status != "Open":
            out.problems.append(
                {
                    "row": row.key,
                    "severity": "warning",
                    "row_data": d,
                    "reason": (
                        f"closed {cs:g} shares of an opening of {os_:g}; imported as fully closed"
                    ),
                }
            )
        if d["strat"] in STOCK_CODES:
            legs, why = _stock(d)
        else:
            legs, why = _legs(d)
        if why or opened is None:
            out.problems.append({"row": row.key, "reason": why or "no opening date", "row_data": d})
            continue
        price = _num(d["open_price"]) or 0.0
        meta = {
            "source": "sheet",
            "row": row.key,
            "spot": _num(d["stock_price"]),
            "bpr": _num(d["bpr"]),
        }
        g = Group(
            key=f"s{row.key}o",
            time=_ts(opened, 15, 0, 1),
            kind="trade",
            # No effect given: an account cannot hold a contract both long and short, so a
            # row opposite an existing holding is read as closing it (buy-backs are
            # sometimes entered as a new P+/C+ row).
            fills=[
                Fill(sym, under, q, price * share, None, 100 if pc else 1, pc)
                for sym, q, share, pc in legs
            ],
            fees=_num(d["open_commission"]) or 0.0,
            meta=meta,
        )
        opens[row.key] = g
        out.groups.append(g)
        closed = _day(d["close_date"])
        cprice = _num(d["close_price"])
        cfees = _num(d["close_commission"]) or 0.0
        exp = _day(d["exp"])
        if status in ("Closed", "Rolled"):
            if closed is None or cprice is None:
                out.problems.append(
                    {
                        "row": row.key,
                        "reason": f"{status} but no closing date or price",
                        "row_data": d,
                    }
                )
                out.groups.remove(g)
                continue
            # A leg opened and closed the same day: the close comes after the open.
            when = _ts(closed, 15, 0, 2) if closed == opened else _ts(closed, 15, 0, 0)
            cg = Group(
                key=f"s{row.key}c",
                time=when,
                kind="trade",
                fills=[
                    Fill(sym, under, -q, cprice * share, "close", 100 if pc else 1, pc)
                    for sym, q, share, pc in legs
                ],
                fees=cfees,
                meta={"source": "sheet", "row": row.key},
            )
            closes.append((row, cg, status))
        elif status in ("Expired", "Assigned"):
            when_day = (closed if status == "Assigned" and closed else exp) or closed or opened
            cg = Group(
                key=f"s{row.key}x",
                time=_ts(when_day, 16, 0, 0),
                kind="expire" if status == "Expired" else "assign",
                fills=[
                    Fill(sym, under, -q, (cprice or 0.0) * share, "close", 100 if pc else 1, pc)
                    for sym, q, share, pc in legs
                    if pc
                ],
                fees=cfees,
                meta={"source": "sheet", "row": row.key},
            )
            if cg.fills:
                closes.append((row, cg, status))
        elif status not in ("Open", ""):
            out.problems.append({"row": row.key, "reason": f"status {status!r}", "row_data": d})

    # A leg marked Rolled was closed and replaced the same day: put the close and the new
    # legs on the same underlying, of the same put/call types, into one roll order.
    removed: set[str] = set()
    by_day: dict[tuple[str, date], list[Group]] = {}
    for g in out.groups:
        if g.key.endswith("o"):
            by_day.setdefault(
                (g.fills[0].underlying, datetime.fromtimestamp(g.time, ET).date()), []
            ).append(g)
    for row, cg, status in closes:
        if status == "Rolled":
            day = datetime.fromtimestamp(cg.time, ET).date()
            types = {f.put_call for f in cg.fills}
            new = [
                g
                for g in by_day.get((cg.fills[0].underlying, day), [])
                if g.key != f"s{row.key}o"
                and {f.put_call for f in g.fills} & types
                and g.key not in removed
            ]
            if new:
                roll = Group(
                    key=f"s{row.key}r",
                    time=cg.time,
                    kind="trade",
                    fills=cg.fills + [f for g in new for f in g.fills],
                    fees=cg.fees + sum(g.fees for g in new),
                    meta={**new[0].meta, "rolled_from": row.key},
                )
                removed.update(g.key for g in new)
                out.groups.append(roll)
                continue
        out.groups.append(cg)
    out.groups = sorted((g for g in out.groups if g.key not in removed), key=lambda g: g.time)
    return out


def _stock(d: dict):
    shares, price = _num(d["shares"]), _num(d["open_price"])
    if not shares or price is None:
        return [], "stock row without shares or price"
    under = str(d["underlying"]).strip().upper()
    return [(under, shares, 1.0, None)], None


def _futures(row: SheetRow) -> dict:
    """Futures options are kept as finished records for the P/L figures only."""
    d = row.data
    opened, closed = _day(d["open_date"]), _day(d["close_date"])
    status = (d["status"] or "").strip()
    pl = _num(d["pl"])
    done = status in ("Closed", "Rolled", "Expired", "Assigned")
    return {
        "id": f"f-{row.key}",
        "underlying": str(d["underlying"]).strip().upper(),
        "strategy": {"P": "Short put", "C": "Short call", "P+": "Long put", "C+": "Long call"}.get(
            d["strat"], d["strat"] or "Futures"
        ),
        "opened_at": _ts(opened, 15, 0) if opened else None,
        "closed_at": _ts(closed or _day(d["exp"]) or opened, 16, 0) if done and opened else None,
        "status": "closed" if done else "open",
        "carried_in": False,
        "futures": True,
        "events": [],
        "realized_pl": pl if done else None,
        "holdings": {},
        "source": "sheet",
    }


# Reconciling --------------------------------------------------------------------------


def reconcile(
    sheet: list[Group], schwab: list[Group], window_start: float, decisions: dict[str, str]
) -> dict:
    """Which sheet events to use, given Schwab's own record from window_start onwards.

    Before the window the sheet is the only record. Inside it, sheet events that match
    Schwab fills are dropped in favour of Schwab's. Sheet events with no match, and Schwab
    fills the sheet does not mention, go to a review list. Accepted sheet-only events are
    used; ignored ones are not.
    """
    pool: dict[tuple[str, int], list[list]] = {}  # (symbol, sign) -> [[time, qty left, key]]
    for g in schwab:
        for f in g.fills:
            pool.setdefault((f.symbol, 1 if f.quantity > 0 else -1), []).append(
                [g.time, abs(f.quantity), g.key]
            )
    last_sheet = max((g.time for g in sheet), default=0.0)
    use, review = [], []
    matched = 0
    for g in sheet:
        if g.time < window_start:
            use.append(g)
            continue
        missing = []
        for f in g.fills:
            cands = pool.get((f.symbol, 1 if f.quantity > 0 else -1), [])
            need = abs(f.quantity)
            for c in sorted(cands, key=lambda c: abs(c[0] - g.time)):
                if abs(c[0] - g.time) > MATCH_DAYS * 86400 or need <= 1e-9:
                    continue
                take = min(need, c[1])
                c[1] -= take
                need -= take
            if need > 1e-9:
                missing.append(f)
        if not missing:
            matched += 1
            continue
        item = f"sheet:{g.key}"
        part = Group(key=g.key, time=g.time, kind=g.kind, fills=missing, fees=g.fees, meta=g.meta)
        decision = decisions.get(item)
        if decision == "accept":
            use.append(part)
        review.append(
            {
                "id": item,
                "kind": "sheet_only",
                "decision": decision,
                "time": g.time,
                "legs": [
                    {"symbol": f.symbol, "quantity": f.quantity, "price": f.price} for f in missing
                ],
                "row": g.meta.get("row"),
            }
        )
    for (symbol, sign), cands in pool.items():
        for t, left, key in cands:
            # Fills after the sheet's last entry are simply newer than the sheet.
            if left > 1e-9 and window_start <= t <= last_sheet:
                item = f"schwab:{key}:{symbol}"
                review.append(
                    {
                        "id": item,
                        "kind": "schwab_only",
                        "decision": decisions.get(item),
                        "time": t,
                        "legs": [{"symbol": symbol, "quantity": sign * left}],
                        "row": None,
                    }
                )
    review.sort(key=lambda r: r["time"])
    return {"use": use, "review": review, "matched": matched}


def window_start_from(schwab: list[Group], configured: float | None) -> float:
    """The start of Schwab's record: the history requested, or the first transaction."""
    first = min((g.time for g in schwab), default=None)
    if configured and first:
        return max(configured, first - 86400)
    return configured or first or datetime.now(UTC).timestamp()


def to_jsonable(g: Group) -> dict:
    return {
        "key": g.key,
        "time": g.time,
        "kind": g.kind,
        "fees": g.fees,
        "meta": g.meta,
        "fills": [vars(f) for f in g.fills],
    }
