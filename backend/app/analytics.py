"""IV, IV rank, beta and buying power ceiling calculations. Pure functions."""

import math
from datetime import date

# tastylive's commonly cited buying power ceilings by VIX level: (VIX below, max share of net liq).
VIX_BP_TIERS = [(15, 0.25), (20, 0.30), (30, 0.35), (40, 0.40), (math.inf, 0.50)]


def bp_ceiling(vix: float | None, scale: float = 1.0) -> float | None:
    """tastytrade's buying-power ceiling for the VIX level; scale 0.5 under portfolio margin."""
    if vix is None:
        return None
    for upper, share in VIX_BP_TIERS:
        if vix < upper:
            return round(share * scale, 4)
    return round(VIX_BP_TIERS[-1][1] * scale, 4)


def _valid_vol(v) -> float | None:
    # Schwab reports missing volatility as -999 or NaN.
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if 0 < v < 1000 and not math.isnan(v) else None


def atm_iv_by_expiration(chain: dict) -> list[tuple[int, float]]:
    """(days to expiration, ATM IV in percent) for each expiration in a Schwab chain."""
    spot = chain.get("underlyingPrice")
    if not spot:
        return []
    per_exp: dict[str, dict] = {}
    for side, key in (("C", "callExpDateMap"), ("P", "putExpDateMap")):
        for exp_key, strikes in (chain.get(key) or {}).items():
            entry = per_exp.setdefault(exp_key, {"days": None, "strikes": {}})
            for strike_s, contracts in strikes.items():
                if not contracts:
                    continue
                c = contracts[0]
                entry["days"] = c.get("daysToExpiration", entry["days"])
                entry["strikes"].setdefault(float(strike_s), {})[side] = _valid_vol(
                    c.get("volatility")
                )
    out = []
    for exp_key, entry in per_exp.items():
        days = entry["days"]
        if days is None:
            try:
                days = int(exp_key.split(":")[1])
            except (IndexError, ValueError):
                continue
        if days <= 0 or not entry["strikes"]:
            continue
        strike = min(entry["strikes"], key=lambda k: abs(k - spot))
        vols = [v for v in entry["strikes"][strike].values() if v is not None]
        if vols:
            out.append((int(days), sum(vols) / len(vols)))
    return sorted(out)


def iv30(points: list[tuple[int, float]], target_days: int = 30) -> float | None:
    """Constant-maturity IV by interpolating total variance between bracketing expirations."""
    if not points:
        return None
    below = [p for p in points if p[0] <= target_days]
    above = [p for p in points if p[0] >= target_days]
    if not below or not above:
        return min(points, key=lambda p: abs(p[0] - target_days))[1]
    (t1, v1), (t2, v2) = below[-1], above[0]
    if t1 == t2:
        return v1
    w1, w2 = (v1 / 100) ** 2 * t1, (v2 / 100) ** 2 * t2
    w = w1 + (w2 - w1) * (target_days - t1) / (t2 - t1)
    return math.sqrt(max(w, 0) / target_days) * 100


def iv_rank(current: float | None, history: list[float]) -> dict | None:
    """Rank and percentile of current against history (both in the same units)."""
    if current is None or not history:
        return None
    lo, hi = min(history), max(history)
    rank = 0.0 if hi == lo else (current - lo) / (hi - lo) * 100
    pct = sum(1 for v in history if v < current) / len(history) * 100
    return {
        "rank": round(max(0.0, min(100.0, rank)), 1),
        "percentile": round(pct, 1),
        "days": len(history),
        "low": round(lo, 2),
        "high": round(hi, 2),
    }


def beta(closes: list[tuple[date, float]], bench: list[tuple[date, float]]) -> float | None:
    """Beta of daily returns against a benchmark, over the dates both have."""
    a, b = dict(closes), dict(bench)
    days = sorted(set(a) & set(b))
    ra, rb = [], []
    for d0, d1 in zip(days, days[1:], strict=False):
        if a[d0] and b[d0]:
            ra.append(a[d1] / a[d0] - 1)
            rb.append(b[d1] / b[d0] - 1)
    if len(ra) < 60:
        return None
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    var = sum((y - mb) ** 2 for y in rb)
    return None if var == 0 else cov / var
