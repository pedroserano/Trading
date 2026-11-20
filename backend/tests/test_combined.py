from types import SimpleNamespace

from app import combined

RULES = SimpleNamespace(
    delta_bias="neutral", delta_min=-0.05, delta_max=0.05, theta_min=0.001, theta_max=0.003
)


def snap(nl, maint, pl, theta, strategies, flags=0):
    return {
        "market": {"spy": 600.0},
        "account": {"net_liq": nl, "maintenance": maint, "cash": 1000.0, "bp_ceiling_pct": 0.5},
        "underlyings": [
            {
                "symbol": "SPY",
                "pl": pl,
                "day_pl": 1.0,
                "delta": 2.0,
                "delta_bw": 2.0,
                "theta": theta,
                "vega": -3.0,
                "gamma": 0.1,
                "strategies": strategies,
            }
        ],
        "totals": {
            "pl": pl,
            "day_pl": 1.0,
            "theta": theta,
            "delta_bw": 2.0,
            "vega": -3.0,
            "gamma": 0.1,
            "flags": flags,
        },
        "error": None,
    }


def st(name, dte):
    return {"name": name, "numbers": {"dte": dte}, "flags": []}


def test_all_adds_up_the_schwab_accounts_and_tags_each_trade():
    a = snap(100_000, 20_000, 50.0, 10.0, [st("Strangle", 40)], flags=1)
    b = snap(50_000, 10_000, -20.0, 5.0, [st("Iron condor", 20)])
    out = combined.combine([("live", "Joint", a), ("live-5678", "Individual", b)], RULES)
    assert out["account"]["net_liq"] == 150_000 and out["account"]["bp_used_pct"] == 0.2
    assert out["totals"]["pl"] == 30.0 and out["totals"]["theta"] == 15.0
    assert out["totals"]["theta_pct_nl"] == 15.0 / 150_000 and out["totals"]["flags"] == 1
    (spy,) = out["underlyings"]
    assert spy["pl"] == 30.0 and spy["delta"] == 4.0
    # Both trades, nearest expiration first, each with its account.
    assert [(s["name"], s["account"], s["account_name"]) for s in spy["strategies"]] == [
        ("Iron condor", "live-5678", "Individual"),
        ("Strangle", "live", "Joint"),
    ]
    assert out["accounts"] == [
        {"key": "live", "name": "Joint"},
        {"key": "live-5678", "name": "Individual"},
    ]
    assert out["bands"]["theta"]["value"] == 15.0 / 150_000
    # The parts are left as they were.
    assert "account" not in a["underlyings"][0]["strategies"][0]


def test_an_unknown_part_leaves_the_total_unknown():
    a = snap(100_000, 20_000, None, 10.0, [])
    b = snap(None, 10_000, -20.0, 5.0, [])
    out = combined.combine([("live", "Joint", a), ("live-5678", "Individual", b)], RULES)
    assert out["totals"]["pl"] is None and out["account"]["net_liq"] is None
    assert combined.combine([("live", "Joint", {"pending": True})], RULES) == {"pending": True}
