"""The analysis dashboard: what worked, what did not, and what went with each.

Two ways of counting P/L are used, deliberately:
- Totals by month, year and underlying count P/L as each leg closes, like the spreadsheet.
  Before Schwab's history starts they are the spreadsheet's own P/L figures.
- The analysis of what worked is per trade chain: a strategy from its opening through every
  roll to its close, with the conditions when it was opened.

Every finding is an association in past trades, not proof of cause, and is shown with its
sample size.
"""

import math
import statistics
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import analytics
from .analysis import TradeLeg, _segment_prob, analyze, bp_estimate
from .chains import realized_by_leg
from .options import parse_option_symbol

ET = ZoneInfo("America/New_York")
INDEX_PRICE_SYMBOL = {
    "SPX": "$SPX",
    "SPXW": "$SPX",
    "XSP": "$XSP",
    "NDX": "$NDX",
    "RUT": "$RUT",
    "VIX": "$VIX",
    "DJX": "$DJX",
}
VIX_PRICED = {"SPX", "SPXW", "XSP", "SPY", "VOO", "IVV"}
MIN_BUCKET = 10


def price_symbol(underlying: str) -> str:
    return INDEX_PRICE_SYMBOL.get(underlying, underlying)


def _d(ts: float | None) -> date | None:
    return None if ts is None else datetime.fromtimestamp(ts, ET).date()


def _close_on(prices: dict[date, float], day: date | None) -> float | None:
    """The close on a day, or the latest close before it within a week."""
    if not prices or day is None:
        return None
    for back in range(0, 8):
        v = prices.get(date.fromordinal(day.toordinal() - back))
        if v is not None:
            return v
    return None


# Facts per trade ----------------------------------------------------------------------


def facts(chain: dict, prices: dict[str, dict[date, float]]) -> dict | None:
    """What is known about one closed trade chain."""
    if chain["status"] != "closed" or chain.get("carried_in") or not chain["events"]:
        return None
    first, last = chain["events"][0], chain["events"][-1]
    opened, closed = _d(chain["opened_at"]), _d(chain["closed_at"])
    meta = first.get("meta") or {}
    source = chain.get("source") or meta.get("source") or "schwab"
    under = chain["underlying"]
    pl = chain["realized_pl"]
    credit = first["net"] if first["net"] > 0 else None
    options = [(parse_option_symbol(x["symbol"]), x) for x in first["legs"]]
    options = [(o, x) for o, x in options if o]
    dte = min(((o.expiration - opened).days for o, _ in options), default=None)
    shorts = [(o, x) for o, x in options if x["quantity"] < 0]

    hist_spot = _close_on(prices.get(price_symbol(under), {}), opened)
    spot = meta.get("spot") or hist_spot
    if spot and shorts and not all(0.5 < o.strike / spot < 2 for o, _ in shorts):
        # A stock price that cannot match the strikes: the spreadsheet's prices are later
        # split-adjusted, and some are missing. Fall back to Schwab's history, else unknown.
        spot = (
            hist_spot
            if (hist_spot and all(0.5 < o.strike / hist_spot < 2 for o, _ in shorts))
            else None
        )
    otm = None
    if spot and shorts:
        dist = []
        for o, _ in shorts:
            d = (spot - o.strike) / spot if o.put_call == "P" else (o.strike - spot) / spot
            dist.append(d)
        otm = min(dist)

    size = meta.get("bpr")
    if size is None and spot and options:
        try:
            tl = [
                TradeLeg(o.put_call, o.strike, o.expiration, x["quantity"], x["price"])
                for o, x in options
            ]
            worst = analyze(
                [TradeLeg(t.put_call, t.strike, t.expiration, t.quantity, 0.0) for t in tl],
                spot,
                opened,
            )
            max_loss = -worst["max_loss"] if worst["max_loss"] is not None else -math.inf
            size = bp_estimate(tl, spot, max_loss)
        except (ValueError, ZeroDivisionError):
            size = None

    vix = prices.get("$VIX", {})
    vix_in, vix_out = _close_on(vix, opened), _close_on(vix, closed)
    up = prices.get(price_symbol(under), {})
    u_in, u_out = spot or _close_on(up, opened), _close_on(up, closed)

    ivr = pop = None
    if under in VIX_PRICED and vix_in and opened:
        hist = [v for d, v in vix.items() if 0 < (opened - d).days <= 365]
        r = analytics.iv_rank(vix_in, hist) if len(hist) > 100 else None
        ivr = r["rank"] if r else None
        if shorts and spot and dte:
            # Chance every short strike finishes out of the money, with VIX as volatility.
            sigma_t = vix_in / 100 * math.sqrt(max(dte, 1) / 365)
            puts = [o.strike for o, _ in shorts if o.put_call == "P"]
            calls = [o.strike for o, _ in shorts if o.put_call == "C"]
            lo = max(puts) if puts else 0.0
            hi = min(calls) if calls else math.inf
            pop = _segment_prob(lo, hi, spot, sigma_t) if hi > lo else None
    snap = meta.get("entry") or {}
    if snap.get("ivr") is not None:
        ivr = snap["ivr"]
    if snap.get("pop") is not None:
        pop = snap["pop"]

    kinds = [e["kind"] for e in chain["events"]]
    if last["kind"] == "expire":
        outcome = "Held to expiration"
    elif last["kind"] in ("assign", "exercise"):
        outcome = "Assigned"
    elif credit and pl is not None and pl < 0:
        outcome = "Closed at a loss"
    elif credit and pl is not None:
        kept = pl / credit
        outcome = (
            "Closed at 75%+ of max"
            if kept >= 0.75
            else "Closed at 50-75% of max"
            if kept >= 0.5
            else "Closed at 25-50% of max"
            if kept >= 0.25
            else "Closed under 25% of max"
        )
    else:
        outcome = "Closed (debit trade)"
    exit_dte = None
    if closed:
        exits = [parse_option_symbol(x["symbol"]) for x in last["legs"]]
        exits = [(o.expiration - closed).days for o in exits if o]
        exit_dte = min(exits) if exits else None
    held = max(((chain["closed_at"] - chain["opened_at"]) / 86400), 0.0)
    # Judged by process only, never by the result: opened at 30-60 DTE and closed before
    # expiration rather than left to expire or be assigned.
    followed = None
    if dte is not None and credit:
        followed = 30 <= dte <= 60 and last["kind"] in ("close", "roll", "adjust")
    return {
        "id": chain["id"],
        "source": source,
        "underlying": under,
        "strategy": chain["strategy"],
        "opened": opened.isoformat() if opened else None,
        "closed": closed.isoformat() if closed else None,
        "pl": pl,
        "credit": credit,
        "kept": (pl / credit) if (credit and pl is not None) else None,
        "days": round(held, 1),
        "rolls": kinds.count("roll"),
        "dte": dte,
        "exit_dte": exit_dte,
        "otm": otm,
        "size": size,
        "vix": vix_in,
        "vix_change": (vix_out - vix_in) if (vix_in and vix_out) else None,
        "move": (u_out / u_in - 1) if (u_in and u_out) else None,
        "ivr": ivr,
        "pop": pop,
        "outcome": outcome,
        "followed": followed,
        "weekday": opened.strftime("%A") if opened else None,
        "year": opened.year if opened else None,
        "earnings_days": snap.get("earnings_days"),
    }


# Breakdowns ---------------------------------------------------------------------------


def _bucket(v, edges: list[tuple[float, str]], last: str):
    if v is None:
        return None
    for upper, label in edges:
        if v < upper:
            return label
    return last


FACTORS = [
    ("strategy", "Strategy", lambda f: f["strategy"]),
    ("underlying", "Underlying", lambda f: f["underlying"]),
    (
        "dte",
        "DTE at entry",
        lambda f: _bucket(
            f["dte"], [(8, "0-7"), (21, "8-20"), (35, "21-34"), (51, "35-50"), (71, "51-70")], "71+"
        ),
    ),
    ("outcome", "How it ended", lambda f: f["outcome"]),
    (
        "exit_dte",
        "DTE left when closed",
        lambda f: _bucket(
            f["exit_dte"], [(1, "0 (expiration day)"), (8, "1-7"), (21, "8-20")], "21+"
        ),
    ),
    ("rolls", "Rolls", lambda f: _bucket(f["rolls"], [(1, "0"), (2, "1"), (3, "2")], "3+")),
    (
        "days",
        "Days held",
        lambda f: _bucket(f["days"], [(3, "0-2"), (8, "3-7"), (15, "8-14"), (31, "15-30")], "31+"),
    ),
    (
        "otm",
        "Distance out of the money at entry",
        lambda f: _bucket(
            f["otm"], [(0, "In the money"), (0.02, "0-2%"), (0.05, "2-5%"), (0.10, "5-10%")], "10%+"
        ),
    ),
    (
        "size",
        "Buying power used",
        lambda f: _bucket(
            f["size"],
            [(1000, "Under $1k"), (5000, "$1k-5k"), (15000, "$5k-15k"), (50000, "$15k-50k")],
            "$50k+",
        ),
    ),
    (
        "vix",
        "VIX at entry",
        lambda f: _bucket(
            f["vix"], [(15, "Under 15"), (20, "15-20"), (25, "20-25"), (30, "25-30")], "30+"
        ),
    ),
    (
        "vix_change",
        "VIX change during the trade",
        lambda f: _bucket(f["vix_change"], [(-3, "Fell 3+"), (3, "Within 3")], "Rose 3+"),
    ),
    (
        "move",
        "Underlying move during the trade",
        lambda f: _bucket(
            f["move"],
            [(-0.10, "Down 10%+"), (-0.03, "Down 3-10%"), (0.03, "Within 3%"), (0.10, "Up 3-10%")],
            "Up 10%+",
        ),
    ),
    (
        "ivr",
        "IV rank at entry (SPX/SPY)",
        lambda f: _bucket(f["ivr"], [(25, "0-25"), (50, "25-50")], "50+"),
    ),
    (
        "pop",
        "Probability of profit at entry",
        lambda f: _bucket(f["pop"], [(0.6, "Under 60%"), (0.7, "60-70%"), (0.8, "70-80%")], "80%+"),
    ),
    (
        "earnings_days",
        "Days to earnings at entry",
        lambda f: _bucket(f["earnings_days"], [(8, "0-7"), (31, "8-30")], "31+"),
    ),
    (
        "followed",
        "Followed the mechanics (opened 30-60 DTE, closed before expiration)",
        lambda f: None if f["followed"] is None else ("Yes" if f["followed"] else "No"),
    ),
    ("weekday", "Day opened", lambda f: f["weekday"]),
    ("year", "Year opened", lambda f: str(f["year"]) if f["year"] else None),
    (
        "source",
        "Recorded by",
        lambda f: {"sheet": "Spreadsheet", "schwab": "Schwab", "sim": "Simulated"}.get(
            f["source"], f["source"]
        ),
    ),
]


def stats(trades: list[dict]) -> dict:
    pls = [t["pl"] for t in trades if t["pl"] is not None]
    wins = [p for p in pls if p > 0]
    losses = [p for p in pls if p <= 0]
    days = sum(max(t["days"], 1.0) for t in trades)
    kept = [t["kept"] for t in trades if t["kept"] is not None]
    return {
        "n": len(pls),
        "win_rate": round(len(wins) / len(pls), 4) if pls else None,
        "total": round(sum(pls), 2),
        "avg": round(sum(pls) / len(pls), 2) if pls else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "per_day": round(sum(pls) / days, 2) if days else None,
        "kept": round(statistics.median(kept), 4) if kept else None,
    }


def breakdown(trades: list[dict], key: str) -> list[dict]:
    fn = next(f for k, _, f in FACTORS if k == key)
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        b = fn(t)
        if b is not None:
            groups[str(b)].append(t)
    rows = [{"bucket": b, **stats(ts)} for b, ts in groups.items()]
    if key in ("underlying", "strategy"):
        rows.sort(key=lambda r: -r["n"])
    else:
        rows.sort(key=lambda r: _order(key, r["bucket"]))
    return rows


ORDER = {
    "dte": ["0-7", "8-20", "21-34", "35-50", "51-70", "71+"],
    "rolls": ["0", "1", "2", "3+"],
    "exit_dte": ["0 (expiration day)", "1-7", "8-20", "21+"],
    "days": ["0-2", "3-7", "8-14", "15-30", "31+"],
    "otm": ["In the money", "0-2%", "2-5%", "5-10%", "10%+"],
    "size": ["Under $1k", "$1k-5k", "$5k-15k", "$15k-50k", "$50k+"],
    "vix": ["Under 15", "15-20", "20-25", "25-30", "30+"],
    "vix_change": ["Fell 3+", "Within 3", "Rose 3+"],
    "move": ["Down 10%+", "Down 3-10%", "Within 3%", "Up 3-10%", "Up 10%+"],
    "ivr": ["0-25", "25-50", "50+"],
    "pop": ["Under 60%", "60-70%", "70-80%", "80%+"],
    "earnings_days": ["0-7", "8-30", "31+"],
    "followed": ["Yes", "No"],
    "weekday": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "outcome": [
        "Closed at 75%+ of max",
        "Closed at 50-75% of max",
        "Closed at 25-50% of max",
        "Closed under 25% of max",
        "Held to expiration",
        "Closed at a loss",
        "Assigned",
        "Closed (debit trade)",
    ],
}


def _order(key, bucket):
    labels = ORDER.get(key)
    if labels and bucket in labels:
        return (0, labels.index(bucket), "")
    return (1, 0, bucket)


def findings(trades: list[dict]) -> list[dict]:
    """Buckets whose results differ from the rest by more than chance would suggest."""
    out = []
    if len(trades) < 3 * MIN_BUCKET:
        return out
    for key, label, fn in FACTORS:
        # Outcome categories describe the result itself, so they explain nothing.
        if key in ("underlying", "year", "source", "outcome"):
            continue
        groups: dict[str, list[float]] = defaultdict(list)
        for t in trades:
            b = fn(t)
            if b is not None and t["pl"] is not None:
                groups[str(b)].append(t["pl"])
        if len(groups) < 2:
            continue
        for b, xs in groups.items():
            rest = [p for o, ys in groups.items() if o != b for p in ys]
            if len(xs) < MIN_BUCKET or len(rest) < MIN_BUCKET:
                continue
            t_stat = _welch(xs, rest)
            if t_stat is None or abs(t_stat) < 2:
                continue
            avg, avg_rest = sum(xs) / len(xs), sum(rest) / len(rest)
            win = sum(1 for p in xs if p > 0) / len(xs)
            win_rest = sum(1 for p in rest if p > 0) / len(rest)
            out.append(
                {
                    "factor": label,
                    "bucket": b,
                    "n": len(xs),
                    "avg": round(avg, 2),
                    "avg_rest": round(avg_rest, 2),
                    "win_rate": round(win, 4),
                    "win_rate_rest": round(win_rest, 4),
                    "total": round(sum(xs), 2),
                    "strength": "strong" if abs(t_stat) >= 3 else "moderate",
                    "better": avg > avg_rest,
                    "t": round(t_stat, 2),
                }
            )
    out.sort(key=lambda f: -abs(f["t"]))
    return out[:20]


def _welch(a: list[float], b: list[float]) -> float | None:
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    return None if se == 0 else (statistics.mean(a) - statistics.mean(b)) / se


# Totals like the spreadsheet ----------------------------------------------------------


def leg_totals(
    chains: list[dict],
    sheet_realized: list[dict],
    window_start: float | None,
    futures: list[dict],
    include_futures: bool,
) -> list[tuple[float, float, str]]:
    """[(time, amount, underlying)] realized leg by leg, sheet figures before the window."""
    out = []
    # Before Schwab's record the sheet's own P/L figures are used, row by row.
    for r in sheet_realized:
        if window_start is None or r["time"] < window_start:
            out.append((r["time"], r["amount"], r["underlying"]))
    # From then on, closes are replayed from the chains' fills.
    for c in chains:
        for t, amt, src in realized_by_leg(c):
            if (src != "sheet") if window_start is None else (t >= window_start):
                out.append((t, amt, c["underlying"]))
    if include_futures:
        for f in futures:
            if f["status"] == "closed" and f["realized_pl"] is not None and f["closed_at"]:
                out.append((f["closed_at"], f["realized_pl"], f["underlying"]))
    return out


def overview(legs: list[tuple[float, float, str]], trades: list[dict]) -> dict:
    by_month: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    by_under: dict[str, float] = defaultdict(float)
    daily: dict[str, float] = defaultdict(float)
    for t, amt, under in legs:
        d = _d(t)
        by_month[d.year][d.month] += amt
        by_under[under] += amt
        daily[d.isoformat()] += amt
    cumulative, run = [], 0.0
    for day in sorted(daily):
        run += daily[day]
        cumulative.append([day, round(run, 2)])
    worst = sorted((t for t in trades if t["pl"] is not None), key=lambda t: t["pl"])[:25]
    return {
        "total": round(sum(a for _, a, _ in legs), 2),
        "months": {y: {m: round(v, 2) for m, v in ms.items()} for y, ms in by_month.items()},
        "underlyings": sorted(
            ([u, round(v, 2)] for u, v in by_under.items()), key=lambda x: -abs(x[1])
        )[:40],
        "cumulative": cumulative,
        "trades": stats(trades),
        "strategies": breakdown(trades, "strategy"),
        "losers": worst,
    }


def build(
    chains: list[dict],
    sheet_realized: list[dict],
    window_start: float | None,
    futures: list[dict],
    prices: dict[str, dict[date, float]],
    *,
    source: str = "all",
    include_futures: bool = False,
) -> dict:
    trades = [f for c in chains if (f := facts(c, prices)) is not None]
    if source != "all":
        trades = [t for t in trades if t["source"] == source]
        chains = (
            [
                c
                for c in chains
                if ((c["events"][0].get("meta") or {}).get("source") or "schwab") == source
            ]
            if chains
            else []
        )
        if source != "sheet":
            sheet_realized = []
        if source == "sheet":
            window_start = None
    legs = leg_totals(chains, sheet_realized, window_start, futures, include_futures)
    return {
        "overview": overview(legs, trades),
        "factors": [
            {"key": k, "label": label, "rows": breakdown(trades, k)} for k, label, _ in FACTORS
        ],
        "findings": findings(trades),
        "mechanics": {
            "followed": stats([t for t in trades if t["followed"] is True]),
            "not_followed": stats([t for t in trades if t["followed"] is False]),
        },
        "coverage": {
            k: sum(1 for t in trades if t[k] is not None)
            for k in ("dte", "otm", "size", "vix", "move", "ivr", "pop", "earnings_days")
        },
        "count": len(trades),
    }
