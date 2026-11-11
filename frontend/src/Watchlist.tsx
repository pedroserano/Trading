import { useEffect, useMemo, useState } from "react";
import { getJson } from "./api";
import { num, pct, signed, tone } from "./format";
import type { WatchRow } from "./types";

type Key = "symbol" | "ivr" | "iv30" | "change" | "em";

const sorters: Record<Key, (r: WatchRow) => number | string> = {
  symbol: (r) => r.symbol,
  ivr: (r) => r.ivr?.rank ?? -1,
  iv30: (r) => r.iv30 ?? -1,
  change: (r) => r.change_pct ?? -999,
  em: (r) => (r.expected_move && r.price ? r.expected_move / r.price : -1),
};

export default function Watchlist() {
  const [rows, setRows] = useState<WatchRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sort, setSort] = useState<{ key: Key; desc: boolean }>({ key: "ivr", desc: true });

  useEffect(() => {
    let stop = false;
    const load = async () => {
      try {
        const r = await getJson<{ rows: WatchRow[] }>("/api/watchlist");
        if (!stop) {
          setRows(r.rows);
          setError(null);
        }
      } catch (e) {
        if (!stop) setError(String((e as Error).message));
      }
    };
    load();
    const t = window.setInterval(load, 10000);
    return () => {
      stop = true;
      window.clearInterval(t);
    };
  }, []);

  const sorted = useMemo(() => {
    if (!rows) return [];
    const f = sorters[sort.key];
    return [...rows].sort((a, b) => {
      const x = f(a);
      const y = f(b);
      const c = x < y ? -1 : x > y ? 1 : 0;
      return sort.desc ? -c : c;
    });
  }, [rows, sort]);

  const th = (key: Key, label: string, title?: string) => (
    <th
      title={title}
      className={`sortable ${key === "symbol" ? "l" : ""}`}
      aria-sort={sort.key === key ? (sort.desc ? "descending" : "ascending") : "none"}
      onClick={() => setSort({ key, desc: sort.key === key ? !sort.desc : key !== "symbol" })}
    >
      {label}
      {sort.key === key ? (sort.desc ? " ↓" : " ↑") : ""}
    </th>
  );

  if (error && !rows) return <div className="banner warn">{error}</div>;
  if (!rows) return <div className="empty">Loading…</div>;
  if (!rows.length) {
    return (
      <div className="empty">
        The watchlist is empty. <a href="#/settings/watchlist">Import from thinkorswim or add symbols</a>
      </div>
    );
  }
  return (
    <>
      <div className="toolbar">
        <span className="dim-text">
          IV and liquidity refresh every 15 minutes. Click a row to open it on the Trade screen.
        </span>
        <a href="#/settings/watchlist" className="toolbar-link">
          Edit watchlist
        </a>
      </div>
      <div className="table-wrap">
        <table className="positions watch">
          <thead>
            <tr>
              {th("symbol", "Symbol")}
              <th>Last</th>
              {th("change", "Chg")}
              {th("iv30", "IV30", "30-day at-the-money implied volatility")}
              {th("ivr", "IVR", "IV rank over up to a year of recorded history")}
              <th title="IV percentile">IVP</th>
              {th("em", "Exp. move", "One standard deviation move to the expiration shown")}
              <th>Exp</th>
              <th title="At-the-money bid-ask spread as a share of the mark">Spread</th>
              <th title="Open interest at the money, call plus put">ATM OI</th>
              <th title="Your position delta and theta in this underlying">Position</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r) => (
              <tr
                key={r.symbol}
                className="strat"
                tabIndex={0}
                onClick={() => (window.location.hash = `#/trade/${encodeURIComponent(r.symbol)}`)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") window.location.hash = `#/trade/${encodeURIComponent(r.symbol)}`;
                }}
              >
                <td className="l">
                  <span className="sym-inline">{r.symbol}</span>
                  {r.error ? (
                    <span className="sub" title={r.error}>
                      no options data
                    </span>
                  ) : null}
                </td>
                <td>{num(r.price, 2)}</td>
                <td className={tone(r.change_pct)}>
                  {r.change_pct === null ? "–" : `${signed(r.change_pct, 2)}%`}
                </td>
                <td>{num(r.iv30, 1)}</td>
                <td className={r.ivr?.rank !== null && r.ivr?.rank !== undefined && r.ivr.rank >= 50 ? "hi" : ""}>
                  {r.ivr?.rank !== null && r.ivr?.rank !== undefined ? Math.round(r.ivr.rank) : "–"}
                  {r.ivr && r.ivr.source === "recorded" && r.ivr.days < 252 ? (
                    <span className="sub"> ({r.ivr.days}d)</span>
                  ) : null}
                </td>
                <td>{r.ivr?.percentile !== null && r.ivr?.percentile !== undefined ? Math.round(r.ivr.percentile) : "–"}</td>
                <td>
                  {r.expected_move === null ? "–" : `±${num(r.expected_move, 2)}`}
                  {r.expected_move && r.price ? (
                    <span className="sub"> {pct(r.expected_move / r.price, 1)}</span>
                  ) : null}
                </td>
                <td>{r.dte === null ? "–" : `${r.dte}d`}</td>
                <td className={r.spread_pct !== null && r.spread_pct > 0.05 ? "warn-text" : ""}>
                  {pct(r.spread_pct, 1)}
                </td>
                <td>{r.atm_oi === null ? "–" : r.atm_oi.toLocaleString("en-US")}</td>
                <td>
                  {r.held ? (
                    <>
                      <span className={tone(r.position_delta)}>Δ {signed(r.position_delta)}</span>
                      <span className="sub"> Θ {signed(r.position_theta)}</span>
                    </>
                  ) : (
                    ""
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
