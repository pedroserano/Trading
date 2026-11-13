"""Selling into a sharp drop: put skew, the drop signal, a possible shock, one-sided trades."""

from datetime import date, timedelta

from app import scanner as sc
from app.signals import drop_signal, one_side, shock, skew

S = sc.merged(None)


def row(k, pd, pm, piv, cd, cm, civ):
    def c(side, d, m, iv):
        return {
            "symbol": f"X {side}{k}",
            "delta": d,
            "mark": m,
            "bid": m - 0.02,
            "ask": m + 0.02,
            "iv": iv,
        }

    return {"strike": k, "put": c("P", -pd, pm, piv), "call": c("C", cd, cm, civ)}


ROWS = [
    row(130, 0.05, 0.60, 62.0, 0.97, 30.0, 40.0),
    row(135, 0.08, 1.00, 58.0, 0.95, 25.0, 40.0),
    row(140, 0.12, 1.60, 55.0, 0.92, 20.0, 40.0),
    row(145, 0.16, 2.40, 52.0, 0.88, 16.0, 40.0),
    row(150, 0.25, 3.60, 50.0, 0.80, 12.0, 41.0),
    row(160, 0.50, 7.00, 46.0, 0.50, 7.00, 44.0),
    row(170, 0.75, 13.0, 44.0, 0.25, 3.00, 42.0),
    row(180, 0.88, 21.0, 43.0, 0.12, 1.20, 41.0),
]


def mid(c):
    return c["mark"]


def test_skew_and_the_drop_signal():
    assert skew(ROWS) == 8.0  # 25-delta put 50 vs 25-delta call 42
    assert drop_signal("down", 86, 8.0, S)
    assert not drop_signal("up", 86, 8.0, S)
    assert not drop_signal("down", 40, 8.0, S)  # IV rank under 60
    assert not drop_signal("down", 86, 2.0, S)  # flat skew


def test_puts_only_outside_the_expected_move():
    # Spot 160, expected move 18: the put must be at 142 or lower, and 16 delta or less.
    legs, label = one_side(ROWS, 160.0, 18.0, "put", True, S, mid)
    assert legs == [{"symbol": "X P140", "quantity": -1, "effect": "open"}]
    assert label.startswith("Short put 140 (outside the ±18.00 expected move")
    legs, label = one_side(ROWS, 160.0, 18.0, "put", False, {**S, "min_ic_credit": 0.1}, mid)
    assert [x["quantity"] for x in legs] == [1, -1] and legs[1]["symbol"] == "X P140"
    assert label.startswith("Put spread 135/140")
    assert one_side(ROWS, 160.0, 40.0, "put", True, S, mid)[0] is None  # nothing that far out


def closes(n=60, start=100.0, step=0.002, last=None):
    out, p, d = [], start, date(2026, 8, 1)
    for i in range(n):
        p *= 1 + (step if i % 2 else -step)
        out.append((d + timedelta(days=i), p))
    if last is not None:
        out.append((d + timedelta(days=n), out[-1][1] * (1 + last)))
    return out


def test_a_possible_shock_from_a_big_day_or_an_iv_jump_but_not_earnings():
    quiet = closes()
    assert shock(quiet, [], None, S) is None
    big = closes(last=-0.09)
    why = shock(big, [], None, S)
    assert why and why.startswith("possible unscheduled shock") and "-9.4%" in why
    # The same day right after an earnings report is the report, not a shock.
    report = big[-1][0] - timedelta(days=1)
    assert shock(big, [], report.isoformat(), S) is None
    ivs = [(date(2026, 9, 1) + timedelta(days=i), 30.0) for i in range(10)]
    ivs += [(date(2026, 9, 11) + timedelta(days=i), 30.0 + 3 * i) for i in range(5)]
    assert "IV30 up" in shock(quiet, ivs, None, S)
