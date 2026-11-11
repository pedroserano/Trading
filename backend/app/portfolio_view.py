"""The Portfolio screen: where buying power, delta and theta sit, and what comes due when.

Built from the Positions snapshot, so it shows exactly what the header and Positions show.
Pure: snapshot and sectors in, a dict out.
"""

import math
from datetime import date, timedelta

from .analysis import TradeLeg, bp_estimate

# Broad groups for funds and indexes; single stocks take their industry from Finnhub's
# company profile when FINNHUB_API_KEY is set.
GROUPS = {
    "Broad index": "SPY SPX XSP $SPX $XSP QQQ NDX $NDX IWM RUT $RUT DIA $DJX VOO IVV VTI EFA EEM",
    "Energy": "XLE XOP OIH USO UNG",
    "Financials": "XLF KRE KBE",
    "Technology": "XLK SMH SOXX IGV",
    "Health care": "XLV XBI IBB",
    "Industrials": "XLI ITA",
    "Consumer discretionary": "XLY XRT",
    "Consumer staples": "XLP",
    "Utilities": "XLU",
    "Materials": "XLB GDX GDXJ",
    "Real estate": "XLRE IYR",
    "Communication": "XLC",
    "Metals": "GLD SLV",
    "Bonds": "TLT IEF HYG LQD",
    "Volatility": "VXX UVXY",
    "Currencies": "FXE FXY UUP",
}
_GROUP_OF = {s: g for g, syms in GROUPS.items() for s in syms.split()}
UNKNOWN = "Single stocks, sector unknown"


def sector(symbol: str, industries: dict | None = None) -> str:
    return _GROUP_OF.get(symbol) or (industries or {}).get(symbol) or UNKNOWN


def _legs(st: dict) -> list[TradeLeg]:
    out = []
    for x in st["legs"]:
        price = x["mark"] if x["mark"] is not None else x["avg_price"]
        if x["kind"] == "OPTION":
            out.append(
                TradeLeg(
                    x["put_call"],
                    x["strike"],
                    date.fromisoformat(x["expiration"]),
                    x["quantity"],
                    price or 0.0,
                )
            )
        else:
            out.append(TradeLeg(None, None, None, x["quantity"], price or 0.0))
    return out


def strategy_bp(st: dict, spot: float | None) -> float | None:
    """Buying power the trade holds: the max loss when defined, else the Reg T estimate."""
    n = st["numbers"]
    if st["defined_risk"] and n.get("max_loss") is not None:
        return max(n["max_loss"], 0.0)
    if not spot:
        return None
    return bp_estimate(_legs(st), spot, -math.inf)


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def build(
    snap: dict, today: date, industries: dict | None = None, manage_dte: int = 21, weeks: int = 10
) -> dict:
    acct = snap.get("account") or {}
    nl = acct.get("net_liq")
    spy = (snap.get("market") or {}).get("spy")

    def share(v):
        return v / nl if (v is not None and nl) else None

    rows, positions, ladder, calendar = [], [], {}, {}
    for u in snap.get("underlyings", []):
        sym, price, beta = u["symbol"], u["price"], u.get("beta")
        bp_total, bp_missing = 0.0, False
        for st in u["strategies"]:
            n = st["numbers"]
            bp = strategy_bp(st, price)
            if bp is None:
                bp_missing = True
            else:
                bp_total += bp
            dbw = (
                n["delta"] * beta * price / spy
                if (n.get("delta") is not None and beta is not None and price and spy)
                else None
            )
            exp = n.get("expiration")
            positions.append(
                {
                    "underlying": sym,
                    "name": st["name"],
                    "trade_id": st.get("trade_id"),
                    "expiration": exp,
                    "dte": n.get("dte"),
                    "delta_bw": None if dbw is None else round(dbw, 1),
                    "delta_pct": share(None if dbw is None else dbw * spy),
                    "theta": n.get("theta"),
                    "bp": None if bp is None else round(bp, 2),
                    "flags": [f["kind"] for f in st["flags"]],
                }
            )
            if exp:
                e = ladder.setdefault(
                    exp,
                    {"expiration": exp, "dte": n.get("dte"), "theta": 0.0, "trades": 0, "bp": 0.0},
                )
                e["trades"] += 1
                e["theta"] += n.get("theta") or 0.0
                e["bp"] += bp or 0.0
                cross = date.fromisoformat(exp) - timedelta(days=manage_dte)
                week = "now" if cross <= today else _monday(cross).isoformat()
                calendar.setdefault(week, []).append(
                    {
                        "underlying": sym,
                        "name": st["name"],
                        "expiration": exp,
                        "dte": n.get("dte"),
                        "crosses": cross.isoformat(),
                    }
                )
        rows.append(
            {
                "symbol": sym,
                "sector": sector(sym, industries),
                "bp": None if bp_missing else round(bp_total, 2),
                "bp_pct": None if bp_missing else share(bp_total),
                "delta_bw": u.get("delta_bw"),
                "delta_pct": share(
                    None if (u.get("delta_bw") is None or not spy) else u["delta_bw"] * spy
                ),
                "theta": u.get("theta"),
                "theta_pct": share(u.get("theta")),
                "trades": len(u["strategies"]),
            }
        )
    rows.sort(key=lambda r: -(r["bp"] or 0))
    sectors: dict = {}
    for r in rows:
        s = sectors.setdefault(
            r["sector"],
            {"sector": r["sector"], "bp": 0.0, "delta_bw": 0.0, "theta": 0.0, "symbols": []},
        )
        s["symbols"].append(r["symbol"])
        s["bp"] += r["bp"] or 0.0
        s["delta_bw"] += r["delta_bw"] or 0.0
        s["theta"] += r["theta"] or 0.0
    for s in sectors.values():
        s["bp_pct"] = share(s["bp"])
        s["delta_pct"] = share(s["delta_bw"] * spy) if spy else None
        s["bp"], s["delta_bw"], s["theta"] = (round(s[k], 2) for k in ("bp", "delta_bw", "theta"))
    first = _monday(today)
    week_list = [
        {"week": "now", "label": f"Inside {manage_dte} DTE now", "trades": calendar.get("now", [])}
    ] + [
        {
            "week": (first + timedelta(weeks=i)).isoformat(),
            "label": f"Week of {(first + timedelta(weeks=i)).isoformat()}",
            "trades": calendar.get((first + timedelta(weeks=i)).isoformat(), []),
        }
        for i in range(weeks)
    ]
    last = (first + timedelta(weeks=weeks)).isoformat()
    later = [t for w, ts in calendar.items() if w != "now" and w >= last for t in ts]
    week_list.append({"week": "later", "label": "Later", "trades": later})
    for e in ladder.values():
        e["theta"], e["bp"] = round(e["theta"], 2), round(e["bp"], 2)
        e["theta_pct"] = share(e["theta"])
    positions.sort(key=lambda p: -abs(p["delta_bw"] or 0))
    return {
        "net_liq": nl,
        "spy": spy,
        "bp_used_pct": acct.get("bp_used_pct"),
        "bp_ceiling_pct": acct.get("bp_ceiling_pct"),
        "bands": snap.get("bands"),
        "totals": snap.get("totals"),
        "manage_dte": manage_dte,
        "underlyings": rows,
        "sectors": sorted(sectors.values(), key=lambda s: -s["bp"]),
        "positions": positions,
        "ladder": sorted(ladder.values(), key=lambda e: e["expiration"]),
        "calendar": week_list,
    }
