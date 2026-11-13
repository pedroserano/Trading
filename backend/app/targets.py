"""Portfolio targets: a beta-weighted delta band and a theta band, as shares of net liq.

tastytrade keeps a portfolio's beta-weighted delta near the bias the trader wants and its
daily theta at about 0.1% to 0.3% of net liq. These are pure functions: the snapshot, the
order ticket and the scanner all use them, and none of them place a hedge on their own.
"""

BIASES = {"neutral": "Neutral", "bullish": "Bullish", "bearish": "Bearish"}
# Our own defaults, not published tastytrade numbers: a neutral book within 5% of net liq
# either side, a leaning book up to 15% toward its side and never the other way.
BIAS_BANDS = {
    "neutral": (-0.05, 0.05),
    "bullish": (0.0, 0.15),
    "bearish": (-0.15, 0.0),
}


def state(value: float | None, lo: float, hi: float) -> str | None:
    if value is None:
        return None
    if value < lo - 1e-12:
        return "below"
    if value > hi + 1e-12:
        return "above"
    return "inside"


def delta_pct(delta_bw: float | None, spy: float | None, net_liq: float | None) -> float | None:
    """Beta-weighted delta (in SPY shares) as dollars per 100% SPY move over net liq."""
    if delta_bw is None or not spy or not net_liq:
        return None
    return delta_bw * spy / net_liq


def bands(totals: dict, account: dict | None, spy: float | None, rules) -> dict:
    nl = (account or {}).get("net_liq")
    d = delta_pct(totals.get("delta_bw"), spy, nl)
    t = totals.get("theta_pct_nl")
    return {
        "bias": rules.delta_bias,
        "delta": {
            "value": d,
            "min": rules.delta_min,
            "max": rules.delta_max,
            "state": state(d, rules.delta_min, rules.delta_max),
        },
        "theta": {
            "value": t,
            "min": rules.theta_min,
            "max": rules.theta_max,
            "state": state(t, rules.theta_min, rules.theta_max),
        },
    }


def lean(b: dict | None) -> str | None:
    """The scanner's lean on new trades: "down" sells the put closer (adds delta, bullish),
    "up" sells the call closer (takes delta away, bearish).

    Outside the delta band, back toward it. Inside it, a bullish or bearish bias keeps
    leaning its way until delta reaches the middle of the band; neutral does not lean.
    """
    d = (b or {}).get("delta") or {}
    st = d.get("state")
    if st in ("below", "above"):
        return {"below": "down", "above": "up"}[st]
    bias, v = (b or {}).get("bias", "neutral"), d.get("value")
    if st == "inside" and v is not None and bias != "neutral":
        mid = (d["min"] + d["max"]) / 2
        if bias == "bullish" and v < mid - 1e-12:
            return "down"
        if bias == "bearish" and v > mid + 1e-12:
            return "up"
    return None


def lean_reason(b: dict) -> str:
    """Why the scanner leaned, for the trade's label."""
    d = b["delta"]
    if d["state"] in ("below", "above"):
        return (
            f"portfolio delta {d['value']:+.1%} of net liq, {d['state']} the "
            f"{d['min']:+.0%} to {d['max']:+.0%} band"
        )
    mid = (d["min"] + d["max"]) / 2
    side = "under" if d["value"] < mid else "over"
    return (
        f"your {b['bias']} bias, portfolio delta {d['value']:+.1%} of net liq, {side} the "
        f"{mid:+.1%} middle of the band"
    )


def after(b: dict | None, d_delta: float | None, d_theta: float | None) -> dict:
    """Where a new trade leaves the bands. d_delta and d_theta are shares of net liq.

    Warns when the trade moves delta or theta further outside its band (or out of it);
    a trade that moves back toward the band is fine even if it does not reach it.
    """
    out = {"delta": None, "theta": None, "warnings": [], "delta_push": False}
    if not b:
        return out
    for key, change in (("delta", d_delta), ("theta", d_theta)):
        band = b.get(key) or {}
        now = band.get("value")
        if now is None or change is None:
            continue
        new = now + change
        out[key] = new
        lo, hi = band["min"], band["max"]
        worse = (new > hi and change > 0) or (new < lo and change < 0)
        if not worse:
            continue
        side = "above" if new > hi else "below"
        edge = hi if new > hi else lo
        what = "Beta-weighted delta" if key == "delta" else "Daily theta"
        f = "+.0%" if key == "delta" else ".2%"
        out["warnings"].append(
            f"{what} would be {new:{f}} of net liq, {side} the {edge:{f}} "
            f"{'bias band' if key == 'delta' else 'theta band'}"
            + (f" (now {now:{f}})" if state(now, lo, hi) != "inside" else "")
        )
        if key == "delta":
            out["delta_push"] = True
    return out
