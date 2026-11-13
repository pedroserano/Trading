"""The largest holdings of SPY and QQQ, so index positions count toward single-stock
concentration ("look-through"). A short SPY put is partly a short NVDA put.

Approximate weights (share of the fund), from the funds' published holdings as of the date
below. Index weights drift with prices and rebalances; these are for sizing guidance, not
accounting. Update AS_OF and the numbers from the issuers' holdings pages when they move.
"""

from .analysis import TradeLeg, analyze

AS_OF = "2026-01"

WEIGHTS: dict[str, dict[str, float]] = {
    "SPY": {
        "NVDA": 0.075,
        "MSFT": 0.065,
        "AAPL": 0.065,
        "AMZN": 0.040,
        "META": 0.030,
        "AVGO": 0.027,
        "GOOGL": 0.022,
        "GOOG": 0.018,
        "TSLA": 0.020,
        "BRK.B": 0.016,
        "JPM": 0.015,
    },
    "QQQ": {
        "NVDA": 0.095,
        "MSFT": 0.085,
        "AAPL": 0.080,
        "AMZN": 0.055,
        "AVGO": 0.050,
        "META": 0.035,
        "TSLA": 0.030,
        "GOOGL": 0.027,
        "GOOG": 0.026,
        "COST": 0.025,
        "NFLX": 0.020,
    },
}


def weight(index: str, symbol: str) -> float:
    return WEIGHTS.get(index, {}).get(symbol, 0.0)


def covered(symbol: str) -> bool:
    """Whether any index here holds the symbol among its largest names."""
    return any(symbol in w for w in WEIGHTS.values())


def look_through(symbol: str, legs, price_of, today, naked_notional) -> tuple[float, float, list]:
    """(buying power, uncovered notional, ["7.5% of your SPY", ...]): the stock's share of
    the account's SPY and QQQ option positions. legs: the account's legs (with underlying,
    is_option, put_call, strike, expiration, quantity, mark); price_of: symbol -> price."""
    bp = notional = 0.0
    via = []
    for idx in WEIGHTS:
        w = weight(idx, symbol)
        mine = [x for x in legs if x.underlying == idx and x.is_option]
        if not w or not mine:
            continue
        notional += w * naked_notional([(x.put_call, x.strike, x.quantity) for x in mine])
        px = price_of(idx)
        if px:
            try:
                bp += (
                    w
                    * analyze(
                        [
                            TradeLeg(x.put_call, x.strike, x.expiration, x.quantity, x.mark or 0)
                            for x in mine
                        ],
                        px,
                        today,
                    )["bp_estimate"]
                )
            except (ValueError, ZeroDivisionError):
                pass
        via.append(f"{w:.1%} of your {idx}")
    return bp, notional, via
