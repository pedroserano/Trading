"""Option chains and expirations from Schwab, reshaped for the trade screen."""

import math
from datetime import date

from .analytics import _valid_vol
from .options import nonstandard


def _f(c: dict, *keys):
    for k in keys:
        v = c.get(k)
        if v is not None:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isnan(f) and f > -999:
                return f
    return None


def is_monthly(d: date) -> bool:
    """Standard monthly expiration: the third Friday (Thursday when Friday is a holiday)."""
    if d.weekday() == 4:
        return 15 <= d.day <= 21
    return d.weekday() == 3 and 14 <= d.day <= 20


def expirations(raw: dict, today: date) -> list[dict]:
    out = []
    for e in (raw or {}).get("expirationList", []):
        try:
            d = date.fromisoformat(str(e.get("expirationDate", ""))[:10])
        except ValueError:
            continue
        dte = (d - today).days
        if dte < 0:
            continue
        monthly = e.get("expirationType") == "M" or is_monthly(d)
        out.append({"date": d.isoformat(), "dte": dte, "monthly": monthly})
    out.sort(key=lambda x: x["date"])
    # Drop duplicates (several option roots can share a date).
    seen, unique = set(), []
    for e in out:
        if e["date"] not in seen:
            seen.add(e["date"])
            unique.append(e)
    return unique


def default_expiration(exps: list[dict], target: int = 45) -> str | None:
    """The monthly closest to the target DTE within 30-60 days, else the closest of any."""
    if not exps:
        return None
    monthlies = [e for e in exps if e["monthly"] and 30 <= e["dte"] <= 60]
    pool = monthlies or exps
    return min(pool, key=lambda e: abs(e["dte"] - target))["date"]


def _contract(c: dict) -> dict:
    bid, ask = _f(c, "bid", "bidPrice"), _f(c, "ask", "askPrice")
    mark = _f(c, "mark", "markPrice")
    if mark is None and bid is not None and ask is not None:
        mark = (bid + ask) / 2
    return {
        "symbol": c.get("symbol"),
        "bid": bid,
        "ask": ask,
        "mark": mark,
        "delta": _f(c, "delta"),
        "gamma": _f(c, "gamma"),
        "theta": _f(c, "theta"),
        "vega": _f(c, "vega"),
        "iv": _valid_vol(c.get("volatility")),
        "oi": _f(c, "openInterest"),
        "volume": _f(c, "totalVolume"),
    }


def _nonstandard(c: dict, underlying: str | None) -> bool:
    """Schwab's own flag, a multiplier other than 100, or a numbered root (AAPL1)."""
    if c.get("nonStandard") or c.get("isNonStandard"):
        return True
    mult = c.get("multiplier")
    if isinstance(mult, int | float) and mult not in (0, 100):
        return True
    return bool(nonstandard(c.get("symbol") or "", underlying))


def chain_rows(raw: dict) -> dict:
    """Rows by strike for one expiration: {strike, call, put}."""
    rows: dict[float, dict] = {}
    dte = None
    for side, key in (("call", "callExpDateMap"), ("put", "putExpDateMap")):
        for exp_key, strikes in (raw.get(key) or {}).items():
            if dte is None:
                try:
                    dte = int(exp_key.split(":")[1])
                except (IndexError, ValueError):
                    pass
            for strike_s, contracts in strikes.items():
                # Adjusted options (after a corporate action) are closing only: never listed.
                contracts = [c for c in contracts or [] if not _nonstandard(c, raw.get("symbol"))]
                if not contracts:
                    continue
                strike = float(strike_s)
                row = rows.setdefault(strike, {"strike": strike, "call": None, "put": None})
                row[side] = _contract(contracts[0])
    out = [rows[k] for k in sorted(rows)]
    spot = _f(raw, "underlyingPrice")
    atm_iv = None
    if spot and out:
        atm = min(out, key=lambda r: abs(r["strike"] - spot))
        vols = [x["iv"] for x in (atm["call"], atm["put"]) if x and x["iv"]]
        atm_iv = sum(vols) / len(vols) if vols else None
    em = None
    if spot and atm_iv and dte is not None:
        em = spot * atm_iv / 100 * math.sqrt(max(dte, 0) / 365)
    return {
        "underlying_price": spot,
        "dte": dte,
        "atm_iv": None if atm_iv is None else round(atm_iv, 2),
        "expected_move": None if em is None else round(em, 2),
        "rows": out,
    }


def atm_liquidity(rows: list[dict], spot: float | None) -> dict:
    """The at-the-money call and put's market: bid/ask as a share of the mark and in dollars
    (their average), and open interest (their sum). None where the chain has no figure."""
    out = {"spread_pct": None, "width": None, "oi": None}
    if not spot or not rows:
        return out
    atm = min(rows, key=lambda r: abs(r["strike"] - spot))
    pcts, widths, ois = [], [], []
    for c in (atm.get("call"), atm.get("put")):
        if c and c.get("bid") is not None and c.get("ask") and c.get("mark"):
            pcts.append((c["ask"] - c["bid"]) / c["mark"])
            widths.append(c["ask"] - c["bid"])
        if c and c.get("oi") is not None:
            ois.append(c["oi"])
    if pcts:
        out["spread_pct"] = round(sum(pcts) / len(pcts), 4)
        out["width"] = round(sum(widths) / len(widths), 2)
    if ois:
        out["oi"] = int(sum(ois))
    return out
