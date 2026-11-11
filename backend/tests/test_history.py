"""Spreadsheet import, reconciliation, dashboard and export. Uses made-up rows only."""

import io
from datetime import UTC, date, datetime

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import chains, dashboard, sheet_import
from app.config import Settings
from app.datasource import DemoDataSource, _demo_txn
from app.history_service import HEADER, export_rows, export_xlsx
from app.main import create_app
from app.options import option_symbol
from app.store import MemoryStore

TRADES_HEADER = [
    "R",
    "Strikes",
    "Strat",
    "Exp",
    "Strike/Diff.",
    "Date",
    "Stock Price",
    "Contracts",
    "Shares",
    "Prem/Price",
    "Commission",
    "P/L",
    "Breakeven",
    "Notional Value",
    "Max Loss",
    "BPR",
    "Account",
    "Status ",
    "Date",
    "Contracts",
    "Shares",
    "Prem/Price",
    "Commission",
    "Total cost",
    "P/L",
    "% Return",
    "Days Held",
    "Multi leg P/L",
    "Cost basis",
]


def d(y, m, dd):
    return datetime(y, m, dd)


def row(
    under,
    strikes,
    strat,
    exp,
    opened,
    shares,
    prem,
    status,
    closed=None,
    cprice=None,
    pl=None,
    account="Joint",
    spot=100.0,
    comm=0.55,
    ccomm=0.55,
    bpr=1000.0,
):
    r = [None] * 29
    r[0], r[1], r[2], r[3], r[4], r[5], r[6] = under, strikes, strat, exp, strikes, opened, spot
    r[7], r[8], r[9], r[10], r[15], r[16], r[17] = 1, shares, prem, comm, bpr, account, status
    r[18], r[20], r[21], r[22], r[24] = closed, shares if closed else None, cprice, ccomm, pl
    return r


ROWS = [
    # A short put closed for a profit.
    row(
        "SPY",
        500,
        "P",
        d(2024, 3, 15),
        d(2024, 2, 1),
        100,
        3.00,
        "Closed",
        d(2024, 2, 20),
        1.40,
        158.9,
    ),
    # A short call rolled out the same day it was replaced.
    row(
        "SPY",
        520,
        "C",
        d(2024, 3, 15),
        d(2024, 2, 1),
        100,
        2.50,
        "Rolled",
        d(2024, 3, 1),
        4.00,
        -151.1,
    ),
    row(
        "SPY",
        530,
        "C",
        d(2024, 4, 19),
        d(2024, 3, 1),
        100,
        4.80,
        "Expired",
        None,
        None,
        479.45,
        ccomm=0,
    ),
    # A put vertical entered as one row with a net price.
    row(
        "QQQ",
        "400/395",
        "Vert P",
        d(2024, 5, 17),
        d(2024, 4, 1),
        200,
        1.50,
        "Closed",
        d(2024, 4, 20),
        0.60,
        177.8,
    ),
    # Stock.
    row("AMD", None, None, None, d(2024, 1, 5), 100, 150.0, "Closed", d(2024, 2, 5), 160.0, 998.9),
    # Futures options: history only.
    row(
        "/ES",
        5000,
        "P",
        d(2024, 3, 15),
        d(2024, 2, 1),
        100,
        10.0,
        "Closed",
        d(2024, 2, 10),
        5.0,
        240.0,
    ),
    # Another account: skipped.
    row(
        "SPY",
        500,
        "P",
        d(2024, 3, 15),
        d(2024, 2, 1),
        100,
        3.0,
        "Closed",
        d(2024, 2, 20),
        1.4,
        158.9,
        account="Individual",
    ),
    # A row with no strike: reported, not imported.
    row(
        "TSLA",
        None,
        "P",
        d(2024, 3, 15),
        d(2024, 2, 1),
        100,
        3.0,
        "Closed",
        d(2024, 2, 20),
        1.0,
        199.0,
    ),
    # Still open.
    row("SPY", 480, "P", d(2026, 12, 18), d(2026, 11, 2), 100, 5.0, "Open"),
]


def workbook(rows=ROWS) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Trades"
    ws.append(TRADES_HEADER)
    for r in rows:
        ws.append(r)
    wb.create_sheet("Summary")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def converted():
    rows, counts = sheet_import.read_workbook(workbook())
    return rows, counts, sheet_import.convert(rows)


# Reading and converting ---------------------------------------------------------------


def test_read_keeps_joint_rows_with_stable_keys():
    rows, counts = sheet_import.read_workbook(workbook())
    assert counts == {"rows": 9, "other_accounts": 1, "account_rows": 8}
    again, _ = sheet_import.read_workbook(workbook())
    assert [r.key for r in rows] == [r.key for r in again]
    assert len({r.key for r in rows}) == 8
    with pytest.raises(sheet_import.SheetError, match="Trades tab"):
        wb = openpyxl.Workbook()
        buf = io.BytesIO()
        wb.save(buf)
        sheet_import.read_workbook(buf.getvalue())
    with pytest.raises(sheet_import.SheetError, match="Excel"):
        sheet_import.read_workbook(b"not a workbook")


def test_convert_rows_into_chains_with_the_roll_linked():
    _, _, conv = converted()
    assert [p["reason"] for p in conv.problems] == ["missing strike or expiration"]
    assert len(conv.futures) == 1 and conv.futures[0]["realized_pl"] == 240.0
    assert sum(r["amount"] for r in conv.realized) == pytest.approx(
        158.9 - 151.1 + 479.45 + 177.8 + 998.9 + 199.0
    )
    res = chains.build(
        conv.groups, {option_symbol("SPY", date(2026, 12, 18), "P", 480): {"quantity": -1}}
    )
    by = {(c["underlying"], c["strategy"]): c for c in res["chains"]}
    call = by[("SPY", "Short call")]
    assert [e["kind"] for e in call["events"]] == ["open", "roll", "expire"]
    assert call["status"] == "closed" and call["source"] == "sheet"
    assert call["realized_pl"] == pytest.approx(250 - 400 + 480 - 0.55 * 3)
    vert = by[("QQQ", "Put vertical")]
    assert vert["events"][0]["net"] == pytest.approx(300.0)  # 1.50 x 2 spreads
    assert by[("AMD", "Stock")]["realized_pl"] == pytest.approx(1000 - 1.10)
    assert by[("SPY", "Short put")]["status"] in ("open", "closed")
    assert res["mismatches"] == []


def test_buy_back_entered_as_a_new_long_row_closes_the_short():
    rows = [
        row("SPY", 500, "P", d(2024, 3, 15), d(2024, 2, 1), 100, 3.0, "Open"),
        row("SPY", 500, "P+", d(2024, 3, 15), d(2024, 2, 9), 100, 1.0, "Open"),
    ]
    conv = sheet_import.convert(sheet_import.read_workbook(workbook(rows))[0])
    (c,) = chains.build(conv.groups, {})["chains"]
    assert c["status"] == "closed" and [e["kind"] for e in c["events"]] == ["open", "close"]


# Reconciling --------------------------------------------------------------------------


def test_reconcile_prefers_schwab_inside_its_window_and_lists_differences():
    _, _, conv = converted()
    put = option_symbol("SPY", date(2024, 3, 15), "P", 500)
    schwab = chains.merge(
        [
            chains.normalize(
                _demo_txn(1, datetime(2024, 2, 1, 15, tzinfo=UTC), [(put, -1, 3.02, "open")])
            ),
            chains.normalize(
                _demo_txn(
                    2,
                    datetime(2024, 2, 8, 15, tzinfo=UTC),
                    [(option_symbol("SPY", date(2024, 3, 15), "P", 490), -1, 1.0, "open")],
                )
            ),
        ]
    )
    window = datetime(2024, 1, 20, tzinfo=UTC).timestamp()
    rec = sheet_import.reconcile(conv.groups, schwab, window, {})
    assert rec["matched"] == 1  # the SPY put opening
    kinds = {r["kind"] for r in rec["review"]}
    assert kinds == {"sheet_only", "schwab_only"}
    sheet_only = [r for r in rec["review"] if r["kind"] == "sheet_only"]
    assert all(r["decision"] is None for r in rec["review"])
    # Nothing unreviewed from inside the window is used; earlier rows are.
    assert all(g.time < window for g in rec["use"])
    accepted = sheet_import.reconcile(conv.groups, schwab, window, {sheet_only[0]["id"]: "accept"})
    assert len(accepted["use"]) == len(rec["use"]) + 1


# Dashboard ----------------------------------------------------------------------------


def test_dashboard_totals_match_the_sheet_and_factors_are_filled():
    _, _, conv = converted()
    res = chains.build(conv.groups, {})
    vix = {
        date(2024, 1, 1).fromordinal(date(2024, 1, 1).toordinal() + i): 14.0 + i % 10
        for i in range(200)
    }
    spy = {k: 500.0 for k in vix}
    out = dashboard.build(
        res["chains"],
        conv.realized,
        None,
        conv.futures,
        {"$VIX": vix, "SPY": spy},
        include_futures=True,
    )
    ov = out["overview"]
    assert ov["total"] == pytest.approx(sum(r["amount"] for r in conv.realized) + 240.0)
    assert ov["months"][2024][2] == pytest.approx(158.9 + 998.9 + 199.0 + 240.0)
    assert out["count"] == 4  # closed chains: put, call chain, vertical, stock
    keys = {f["key"] for f in out["factors"]}
    assert {"dte", "otm", "vix", "outcome", "followed", "exit_dte"} <= keys
    dte = next(f for f in out["factors"] if f["key"] == "dte")["rows"]
    assert [r["bucket"] for r in dte] == sorted(
        [r["bucket"] for r in dte], key=lambda b: dashboard.ORDER["dte"].index(b)
    )
    assert out["coverage"]["vix"] >= 2


def test_findings_need_enough_trades_and_ignore_outcome_labels():
    trades = []
    for i in range(40):
        trades.append(
            {
                "pl": 100.0 if i % 2 else 120.0,
                "days": 10,
                "kept": 0.5,
                "strategy": "Strangle",
                "underlying": "SPY",
                "dte": 45,
                "exit_dte": 21,
                "outcome": "Closed at 50-75% of max",
                "rolls": 0,
                "otm": 0.06,
                "size": 2000,
                "vix": 22,
                "vix_change": 0,
                "move": 0,
                "ivr": None,
                "pop": None,
                "earnings_days": None,
                "followed": True,
                "weekday": "Monday",
                "year": 2024,
                "source": "sheet",
            }
        )
    for i in range(40):
        trades.append({**trades[0], "pl": -300.0 + i, "vix": 12, "followed": False})
    found = dashboard.findings(trades)
    assert found and found[0]["strength"] == "strong"
    assert all(f["factor"] != "How it ended" for f in found)
    assert any(
        f["factor"] == "VIX at entry" and f["bucket"] == "Under 15" and not f["better"]
        for f in found
    )
    assert dashboard.findings(trades[:10]) == []


# Export -------------------------------------------------------------------------------


def test_export_rows_in_the_sheet_layout():
    put, call = (
        option_symbol("SPY", date(2026, 12, 18), "P", 480),
        option_symbol("SPY", date(2026, 12, 18), "C", 520),
    )
    txns = [
        _demo_txn(
            1,
            datetime(2026, 11, 2, 15, tzinfo=UTC),
            [(put, -1, 5.0, "open"), (call, -1, 4.0, "open")],
        ),
        _demo_txn(2, datetime(2026, 11, 9, 15, tzinfo=UTC), [(put, 1, 2.0, "close")]),
    ]
    res = chains.build(chains.merge([chains.normalize(t) for t in txns]), {call: {"quantity": -1}})
    rows = export_rows(res["chains"], date(2026, 11, 1), date(2026, 11, 30))
    assert len(rows) == 2
    closed = next(r for r in rows if r[17] == "Closed")
    assert closed[:4] == ["SPY", 480.0, "P", date(2026, 12, 18)]
    assert closed[8] == 100 and closed[9] == 5.0 and closed[21] == 2.0
    assert closed[24] == pytest.approx(300 - 0.65 - 0.65)
    still = next(r for r in rows if r[17] == "Open")
    assert still[2] == "C" and still[18] is None
    wb = openpyxl.load_workbook(io.BytesIO(export_xlsx(rows)))
    assert [c.value for c in wb["Trades"][1]] == HEADER
    assert wb["Trades"].max_row == 3


# API ----------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    s = Settings(
        _env_file=None,
        app_password="p" * 12,
        secret_key="s" * 32,
        demo_mode=True,
        cookie_secure=False,
        data_dir=tmp_path,
    )
    app = create_app(
        s,
        store=MemoryStore(),
        start_background=False,
        static_dir=tmp_path / "x",
        source=DemoDataSource(today=date(2026, 11, 11)),
    )
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": "p" * 12})
        yield c


def test_import_dashboard_review_and_export_endpoints(client):
    r = client.post(
        "/history/import", files={"file": ("book.xlsx", workbook(), "application/octet-stream")}
    )
    assert r.status_code == 303
    page = client.get("/connect").text
    assert "Imported 8 Joint rows: 8 new" in page and "1 rows for other accounts" in page
    again = client.post("/history/import", files={"file": ("book.xlsx", workbook(), "x")})
    assert again.status_code == 303 and "8 unchanged" in client.get("/connect").text
    h = client.get("/api/history").json()
    assert h["rows"] == 8 and len(h["problems"]) == 1
    act = client.get("/api/activity").json()
    counts = act["trade_counts"]
    assert counts["sheet"] >= 4 and counts["all"] == counts["sheet"] + counts.get("schwab", 0)
    only = client.get("/api/activity?source=sheet&limit=2").json()
    assert len(only["trades"]) == 2 and only["trades_shown_of"] == counts["sheet"]
    assert {t["source"] for t in only["trades"]} == {"sheet"}
    dash = client.get("/api/dashboard?futures=true").json()
    assert dash["count"] >= 4 and dash["overview"]["total"] > 0
    assert client.get("/api/dashboard?account=sim").json()["count"] == 0
    assert client.post("/api/history/review", json={"id": "x", "decision": "ignore"}).json() == {
        "ok": True
    }
    bad = client.post("/history/import", files={"file": ("x.xlsx", b"nope", "x")})
    assert bad.status_code == 303 and "Excel" in client.get("/connect").text
    exp = client.get("/api/export/sheet?start=2020-01-01&end=2030-01-01")
    assert exp.status_code == 200 and exp.content[:2] == b"PK"
