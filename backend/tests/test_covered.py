"""Covered calls adjust the shares' cost basis: a credit lowers it as the call is sold, a
buyback or roll for a debit raises it again."""

from test_phase3 import client  # noqa: F401  (fixture)

from app.chains import covered_premium

CALL = "AAPL  261218C00250000"
CALL2 = "AAPL  270115C00255000"
PUT = "AAPL  261218P00200000"


def ev(legs, fee=0.0, kind="open"):
    return {
        "kind": kind,
        "commission": fee,
        "legs": [{"symbol": s, "quantity": q, "price": p} for s, q, p in legs],
    }


def chain(cid, strategy, opened, events, status="open"):
    return {
        "id": cid,
        "underlying": "AAPL",
        "strategy": strategy,
        "opened_at": opened,
        "status": status,
        "closed_at": None,
        "events": events,
    }


def test_credits_count_when_sold_and_debits_raise_the_basis_again():
    chains = [
        # Sold for 3.00, then rolled out for a 0.50 debit: 250 net, less fees.
        chain(
            "a",
            "Short call",
            200,
            [
                ev([(CALL, -1, 3.00)], fee=0.65),
                ev([(CALL, 1, 4.00), (CALL2, -1, 3.50)], fee=1.30, kind="roll"),
            ],
        ),
    ]
    out = covered_premium(chains, "AAPL", "long", since=100)
    assert out["premium"] == round(300 - 50 - 1.95, 2)
    assert [c["id"] for c in out["chains"]] == ["a"]


def test_a_buy_write_counts_only_its_option_leg():
    chains = [
        chain(
            "b",
            "Covered call",
            200,
            [
                ev([("AAPL", 100, 240.0), (CALL, -1, 5.0)], fee=1.0),
            ],
        )
    ]
    out = covered_premium(chains, "AAPL", "long", since=100)
    # 500 credit, and the option's share of the fee (1 of 101 units).
    assert out["premium"] == round(500 - 1.0 / 101, 2)


def test_only_calls_written_while_holding_and_never_spreads_or_zebras():
    chains = [
        chain("old", "Short call", 50, [ev([(CALL, -1, 2.0)])]),  # before these shares
        chain("zeb", "Call ZEBRA", 200, [ev([(CALL, 2, 9.0), (CALL2, -1, 6.0)])]),
        chain("vert", "Call vertical", 200, [ev([(CALL, -1, 3.0), (CALL2, 1, 1.0)])]),
        chain("put", "Short put", 200, [ev([(PUT, -1, 2.0)])]),
        chain("ok", "Short call", 150, [ev([(CALL, -1, 1.5)])]),
        {**chain("other", "Short call", 200, [ev([(CALL, -1, 9.0)])]), "underlying": "MSFT"},
    ]
    out = covered_premium(chains, "AAPL", "long", since=100)
    assert [c["id"] for c in out["chains"]] == ["ok"] and out["premium"] == 150.0
    # Short shares: covered puts instead.
    short = covered_premium(chains, "AAPL", "short", since=100)
    assert [c["id"] for c in short["chains"]] == ["put"] and short["premium"] == 200.0


def shares_row(tid, qty, avg):
    return {
        "trade_id": tid,
        "legs": [{"kind": "EQUITY", "symbol": "AAPL", "quantity": qty, "avg_price": avg}],
        "numbers": {},
    }


def test_snapshot_and_history_show_the_adjusted_basis(client):  # noqa: F811
    pf = client.app.state.accounts["portfolios"]["sim"]
    chains = [
        chain("c1", "Short call", 200, [ev([(CALL, -1, 3.0)])]),
        chain("c2", "Short call", 300, [ev([(CALL2, -2, 2.0)])], status="closed"),
    ]
    pf.trade_info = lambda: {"s1": {"opened_at": 150}, "s2": {"opened_at": 250}}
    pf.chain_list = lambda: chains
    # Two purchases: 100 at 240, 100 at 250. Calls since the first: 300 + 400 = 700.
    rows = [shares_row("s1", 100, 240.0), shares_row("s2", 100, 250.0)]
    pf._adjust_basis("AAPL", rows)
    for r in rows:
        n = r["numbers"]
        assert n["covered_premium"] == 700.0 and n["covered_count"] == 2
        assert n["adjusted_basis"] == 245.0 - 3.5 and n["covered_since"] == 150

    pf.snapshot = {"underlyings": [{"symbol": "AAPL", "strategies": rows}]}
    r = client.get("/api/covered?account=sim&symbol=AAPL").json()
    assert r["shares"] == 200 and r["avg_price"] == 245.0
    assert r["premium"] == 700.0 and r["per_share"] == 3.5 and r["adjusted"] == 241.5
    assert [c["id"] for c in r["chains"]] == ["c1", "c2"]
    assert client.get("/api/covered?account=sim&symbol=QQQ").status_code == 404


def test_short_shares_add_covered_put_premium(client):  # noqa: F811
    pf = client.app.state.accounts["portfolios"]["sim"]
    pf.trade_info = lambda: {"s1": {"opened_at": 150}}
    pf.chain_list = lambda: [chain("p", "Short put", 200, [ev([(PUT, -1, 2.0)])])]
    rows = [shares_row("s1", -100, 240.0)]
    pf._adjust_basis("AAPL", rows)
    assert rows[0]["numbers"]["adjusted_basis"] == 242.0  # sold at 240, plus 2 a share
