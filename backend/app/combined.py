"""All accounts together: the Schwab accounts' snapshots as one, for the "All" picker.

The simulated account is never included: All is real money only. Every strategy keeps the
account it is in (`account`, `account_name`), so its actions go to that account.
"""

from __future__ import annotations

import copy

from . import targets

ALL = "all"

_ACCOUNT_SUMS = (
    "net_liq",
    "cash",
    "sweep",
    "margin_balance",
    "buying_power",
    "maintenance",
    "free_cash",
)
_UNDERLYING_SUMS = ("pl", "day_pl", "delta", "delta_bw", "theta", "vega", "gamma")
_TOTAL_SUMS = ("pl", "day_pl", "theta", "delta_bw", "vega", "gamma")


def _sum(values: list) -> float | None:
    """The sum, or None when any part is unknown (a total that silently skips one would
    mislead)."""
    if not values or any(v is None for v in values):
        return None
    return sum(values)


def combine(parts: list[tuple[str, str, dict]], rules) -> dict:
    """One snapshot from (key, name, snapshot) for each Schwab account, the first first.
    Rules (the first account's) set the bands."""
    snaps = [(k, n, s) for k, n, s in parts if s and not s.get("pending")]
    if not snaps:
        return {"pending": True}
    out = copy.deepcopy(snaps[0][2])
    out["accounts"] = [{"key": k, "name": n} for k, n, _ in snaps]

    accts = [s.get("account") for _, _, s in snaps]
    if all(a is None for a in accts):
        out["account"] = None
    else:
        first = next(a for a in accts if a)
        acct = {
            "last4": "",
            "type": None,
            "bp_ceiling_pct": first.get("bp_ceiling_pct"),
            "margin_type": first.get("margin_type"),
        }
        for f in _ACCOUNT_SUMS:
            acct[f] = _sum([(a or {}).get(f) for a in accts])
        nl, maint = acct["net_liq"], acct["maintenance"]
        acct["bp_used_pct"] = (maint / nl) if (maint is not None and nl) else None
        out["account"] = acct

    by_symbol: dict[str, dict] = {}
    for key, name, s in snaps:
        for u in s.get("underlyings") or []:
            strategies = [
                {**st, "account": key, "account_name": name} for st in u.get("strategies") or []
            ]
            have = by_symbol.get(u["symbol"])
            if have is None:
                by_symbol[u["symbol"]] = {**copy.deepcopy(u), "strategies": strategies}
                continue
            for f in _UNDERLYING_SUMS:
                have[f] = _sum([have.get(f), u.get(f)])
            have["strategies"] = have["strategies"] + strategies
    underlyings = list(by_symbol.values())
    for u in underlyings:
        for f in ("pl", "day_pl", "theta", "gamma"):
            if u.get(f) is not None:
                u[f] = round(u[f], 2)
        for f in ("delta", "delta_bw", "vega"):
            if u.get(f) is not None:
                u[f] = round(u[f], 1)
        u["strategies"].sort(key=lambda st: (st["numbers"]["dte"] is None, st["numbers"]["dte"]))
    out["underlyings"] = underlyings

    totals = {f: _sum([(s.get("totals") or {}).get(f) for _, _, s in snaps]) for f in _TOTAL_SUMS}
    nl = (out["account"] or {}).get("net_liq")
    totals["theta_pct_nl"] = totals["theta"] / nl if (nl and totals["theta"] is not None) else None
    totals["flags"] = sum((s.get("totals") or {}).get("flags", 0) for _, _, s in snaps)
    out["totals"] = totals
    spy = (out.get("market") or {}).get("spy")
    out["bands"] = targets.bands(totals, out["account"], spy, rules)
    errors = [f"{n}: {s['error']}" for _, n, s in snaps if s.get("error")]
    out["error"] = "; ".join(errors) or None
    return out


def tag(rows: list[dict], key: str, name: str) -> list[dict]:
    return [{**r, "account": key, "account_name": name} for r in rows]
