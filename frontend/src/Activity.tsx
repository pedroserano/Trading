import OrderCheck from "./OrderCheck";
import { useCallback, useContext, useEffect, useState } from "react";
import { getJson, postJson } from "./api";
import { ChainTimeline, chainSummary, legText, when } from "./ChainHistory";
import { AccountContext } from "./context";
import { money, pct, signed, tone } from "./format";
import type { Activity as ActivityData, SimOrder, SimTrade } from "./types";

function net(v: number | null): string {
  if (v === null) return "–";
  return `${Math.abs(v).toFixed(2)} ${v > 0 ? "cr" : v < 0 ? "db" : ""}`;
}

export default function Activity() {
  const account = useContext(AccountContext);
  const [data, setData] = useState<ActivityData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [limit, setLimit] = useState(300);

  const load = useCallback(async () => {
    try {
      setData(await getJson<ActivityData>(`/api/activity?account=${account}&limit=${limit}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [account, limit]);

  useEffect(() => {
    setData(null);
    load();
    const t = window.setInterval(load, 5000);
    return () => window.clearInterval(t);
  }, [load]);

  const cancel = async (o: SimOrder) => {
    try {
      if (account === "live" && !window.confirm(`Cancel order #${o.id} at Schwab?`)) return;
      await postJson(`/api/orders/${o.id}/cancel?account=${account}`, {});
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const reset = async () => {
    const amount = window.prompt("Reset the simulated account. Starting cash:", String(data?.start_cash ?? 100000));
    if (amount === null) return;
    const confirm = window.prompt("This deletes all simulated orders, positions and the journal. Type RESET to confirm.");
    if (confirm !== "RESET") return;
    try {
      await postJson("/api/sim/reset", { start_cash: Number(amount), confirm });
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!data) return error ? <div className="banner warn">{error}</div> : <div className="empty">Loading…</div>;
  const live = data.account === "live";
  const working = data.orders.filter((o) => o.status === "WORKING");
  const done = data.orders.filter((o) => o.status !== "WORKING").slice(0, 50);
  const realized = data.trades.reduce((a, t) => a + (t.realized_pl ?? 0), 0);

  return (
    <div className="activity">
      {error ? <div className="banner warn">{error}</div> : null}
      {live ? (
        <div className="sim-summary">
          <span className="badge live">SCHWAB ACCOUNT</span>
          <span>
            <span className="lbl">History synced</span> {data.synced_at ? when(data.synced_at) : "not yet"}
          </span>
          <span>
            <span className="lbl">Realized</span> <span className={tone(realized)}>{signed(realized)}</span>
          </span>
          <span className="dim-text">Built from Schwab's transactions, including trades placed in thinkorswim.</span>
        </div>
      ) : (
        <div className="sim-summary">
          <span className="badge sim">SIMULATED ACCOUNT</span>
          <span>
            <span className="lbl">Net liq</span> {money(data.net_liq ?? null)}
          </span>
          <span>
            <span className="lbl">Cash</span> {money(data.cash ?? null)}
          </span>
          <span>
            <span className="lbl">Since start</span>{" "}
            <span className={tone(data.net_liq == null || data.start_cash == null ? null : data.net_liq - data.start_cash)}>
              {data.net_liq == null || data.start_cash == null ? "–" : signed(data.net_liq - data.start_cash)}
            </span>
          </span>
          <span>
            <span className="lbl">Realized</span> <span className={tone(realized)}>{signed(realized)}</span>
          </span>
          <button type="button" className="link reset" onClick={reset}>
            Reset simulated account
          </button>
        </div>
      )}

      {live && data.error ? <div className="msg warn">History sync: {data.error}</div> : null}
      {live && data.mismatches?.length ? (
        <div className="msg warn">
          The trade history does not fully explain what Schwab holds now:{" "}
          {data.mismatches.map((m) => `${legText(m.symbol, m.schwab)} (history says ${signed(m.chains)})`).join("; ")}. It usually settles after the next sync.
        </div>
      ) : null}

      {live && data.live ? (
        <div className={`msg ${data.live.on ? "warn" : ""}`}>
          Live trading is <b>{data.live.on ? "on" : "off"}</b>
          {data.live.on
            ? `: ${data.live.symbols.join(", ")}, up to ${data.live.max_contracts} contract${data.live.max_contracts > 1 ? "s" : ""} per leg.`
            : data.live.demo
              ? " (demo mode)."
              : !data.live.server_allowed
                ? ": LIVE_TRADING=true is not set in the server's .env file."
                : ": switch it on in Settings."}{" "}
          <a href="#/settings/live">Live trading settings</a>
          {data.live.error ? <span className="sub"> Reading orders from Schwab: {data.live.error}</span> : null}
        </div>
      ) : null}
      <h3>{live ? "Working orders at Schwab" : "Working orders"}</h3>
      {working.length ? (
        <OrderTable orders={working} onCancel={cancel} />
      ) : (
        <p className="dim-text">
          {live
            ? "None. Orders placed in thinkorswim show here too."
            : "None. Place simulated orders from the Trade screen, or close and roll from Positions."}
        </p>
      )}

      <h3>Trades</h3>
      {live && data.trades_shown_of ? (
        <div className="toolbar">
          <span className="dim-text">
            Showing {data.trades.length.toLocaleString("en-US")} of {data.trades_shown_of.toLocaleString("en-US")}: open
            trades first, then the most recently closed.
          </span>
        </div>
      ) : null}
      {live && data.sheet?.error ? <div className="msg warn">{data.sheet.error}</div> : null}
      {data.trades.length ? (
        <div className="table-wrap">
          <table className="positions">
            <thead>
              <tr>
                <th className="l">Trade</th>
                <th>Opened</th>
                <th>Closed</th>
                <th>Rolls</th>
                <th>Net so far</th>
                <th>Realized P/L</th>
                <th className="l">Status</th>
              </tr>
            </thead>
            <tbody>
              {data.trades.map((t) => (
                <TradeRows
                  key={String(t.id)}
                  t={t}
                  account={account}
                  onChanged={load}
                  open={open.has(String(t.id))}
                  toggle={() =>
                    setOpen((s) => {
                      const n = new Set(s);
                      const k = String(t.id);
                      if (n.has(k)) n.delete(k);
                      else n.add(k);
                      return n;
                    })
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="dim-text">{live ? "No trades found in the Schwab history yet." : "No trades yet."}</p>
      )}
      {live && data.trades_shown_of && data.trades.length < data.trades_shown_of ? (
        <p>
          <button type="button" className="small" onClick={() => setLimit(limit + 500)}>
            Show 500 more
          </button>
        </p>
      ) : null}

      <h3>Results by strategy</h3>
      {data.stats.length ? (
        <div className="table-wrap">
          <table className="positions">
            <thead>
              <tr>
                <th className="l">Strategy</th>
                <th>Closed trades</th>
                <th>Win rate</th>
                <th>Total P/L</th>
                <th>Average P/L</th>
                <th>Average days</th>
                <th>P/L per day</th>
              </tr>
            </thead>
            <tbody>
              {data.stats.map((s) => (
                <tr key={s.strategy}>
                  <td className="l">{s.strategy}</td>
                  <td>{s.trades}</td>
                  <td>{pct(s.win_rate)}</td>
                  <td className={tone(s.total_pl)}>{signed(s.total_pl)}</td>
                  <td className={tone(s.avg_pl)}>{signed(s.avg_pl)}</td>
                  <td>{s.avg_days}</td>
                  <td className={tone(s.pl_per_day)}>{signed(s.pl_per_day, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="dim-text">Statistics appear once trades are closed.</p>
      )}

      {live ? <OrderCheck /> : null}

      <h3>{live ? "Orders at Schwab, last 7 days" : "Order history"}</h3>
      {done.length ? <OrderTable orders={done} /> : <p className="dim-text">No orders yet.</p>}
    </div>
  );
}

function OrderTable({ orders, onCancel }: { orders: SimOrder[]; onCancel?: (o: SimOrder) => void }) {
  return (
    <div className="table-wrap">
      <table className="positions orders">
        <thead>
          <tr>
            <th className="l">#</th>
            <th className="l">Placed</th>
            <th className="l">Order</th>
            <th className="l">Legs</th>
            <th>Limit</th>
            <th className="l">Status</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <tr key={o.id}>
              <td className="l">{o.id}</td>
              <td className="l">{when(o.created_at)}</td>
              <td className="l">
                <span className="sym-inline">{o.underlying}</span>
                {[o.intent, o.strategy, o.duration].filter(Boolean).join(" · ")}
                {o.placed_by_app ? <span className="sub"> · placed by the app</span> : null}
                {o.reason ? <div className="sub">{o.reason}</div> : null}
              </td>
              <td className="l legs-cell">
                {o.legs.map((l) => (
                  <div key={l.symbol}>{legText(l.symbol, l.quantity)}</div>
                ))}
              </td>
              <td>{net(o.limit_net)}</td>
              <td className={`l status ${o.status.toLowerCase()}`}>
                {(o.schwab_status ?? o.status).toLowerCase().replace(/_/g, " ")}
                {o.filled_at ? <div className="sub">{when(o.filled_at)}</div> : null}
              </td>
              <td>
                {onCancel ? (
                  <button type="button" className="small" onClick={() => onCancel(o)}>
                    Cancel
                  </button>
                ) : o.schwab_json === undefined ? null : (
                  <details className="json inline">
                    <summary>JSON</summary>
                    <pre>{JSON.stringify(o.schwab_json, null, 2)}</pre>
                  </details>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TradeRows(props: { t: SimTrade; account: "live" | "sim"; open: boolean; toggle: () => void; onChanged: () => void }) {
  const { t, open, toggle } = props;
  const sum = chainSummary(t);
  return (
    <>
      <tr className="strat" onClick={toggle} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && toggle()} aria-expanded={open}>
        <td className="l name">
          <span className="caret">{open ? "▾" : "▸"}</span>
          <span className="sym-inline">{t.underlying}</span>
          {t.strategy}
          {t.carried_in ? (
            <span className="sub" title="Held before the transaction history starts">
              {" "}
              carried in
            </span>
          ) : null}
        </td>
        <td>{t.carried_in ? `before ${when(t.opened_at)}` : when(t.opened_at)}</td>
        <td>{when(t.closed_at)}</td>
        <td>{sum.rolls}</td>
        <td className={tone(sum.net)}>{signed(sum.net)}</td>
        <td className={tone(t.realized_pl)}>{t.realized_pl === null ? "–" : signed(t.realized_pl)}</td>
        <td className="l">{t.status}</td>
      </tr>
      {open ? (
        <tr className="leg">
          <td colSpan={7} className="l">
            <ChainTimeline chain={t} account={props.account} onChanged={props.onChanged} />
          </td>
        </tr>
      ) : null}
    </>
  );
}
