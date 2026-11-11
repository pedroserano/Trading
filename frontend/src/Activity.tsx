import { useCallback, useEffect, useState } from "react";
import { getJson, postJson } from "./api";
import { money, pct, shortDate, signed, strike, tone } from "./format";
import type { Activity as ActivityData, SimOrder, SimTrade } from "./types";

function when(ts: number | null): string {
  if (!ts) return "–";
  return new Date(ts * 1000).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function legText(symbol: string, quantity: number): string {
  // "SPY   261218P00555000" -> "-1 Dec 18 555 P"
  const m = symbol.match(/^(.{1,6}?)\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/);
  if (!m) return `${signed(quantity)} ${symbol}`;
  const exp = `20${m[2]}-${m[3]}-${m[4]}`;
  return `${signed(quantity)} ${shortDate(exp)} ${strike(Number(m[6]) / 1000)} ${m[5]}`;
}

function net(v: number | null): string {
  if (v === null) return "–";
  return `${Math.abs(v).toFixed(2)} ${v > 0 ? "cr" : v < 0 ? "db" : ""}`;
}

export default function Activity() {
  const [data, setData] = useState<ActivityData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<number>>(new Set());

  const load = useCallback(async () => {
    try {
      setData(await getJson<ActivityData>("/api/activity"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 5000);
    return () => window.clearInterval(t);
  }, [load]);

  const cancel = async (o: SimOrder) => {
    try {
      await postJson(`/api/orders/${o.id}/cancel`, {});
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
  const working = data.orders.filter((o) => o.status === "WORKING");
  const done = data.orders.filter((o) => o.status !== "WORKING").slice(0, 50);
  const realized = data.trades.reduce((a, t) => a + (t.realized_pl ?? 0), 0);

  return (
    <div className="activity">
      {error ? <div className="banner warn">{error}</div> : null}
      <div className="sim-summary">
        <span className="badge sim">SIMULATED ACCOUNT</span>
        <span>
          <span className="lbl">Net liq</span> {money(data.net_liq)}
        </span>
        <span>
          <span className="lbl">Cash</span> {money(data.cash)}
        </span>
        <span>
          <span className="lbl">Since start</span>{" "}
          <span className={tone(data.net_liq === null ? null : data.net_liq - data.start_cash)}>
            {data.net_liq === null ? "–" : signed(data.net_liq - data.start_cash)}
          </span>
        </span>
        <span>
          <span className="lbl">Realized</span> <span className={tone(realized)}>{signed(realized)}</span>
        </span>
        <button type="button" className="link reset" onClick={reset}>
          Reset simulated account
        </button>
      </div>

      <h3>Working orders</h3>
      {working.length ? (
        <OrderTable orders={working} onCancel={cancel} />
      ) : (
        <p className="dim-text">None. Place simulated orders from the Trade screen, or close and roll from Positions.</p>
      )}

      <h3>Trades</h3>
      {data.trades.length ? (
        <div className="table-wrap">
          <table className="positions">
            <thead>
              <tr>
                <th className="l">Trade</th>
                <th>Opened</th>
                <th>Closed</th>
                <th>Rolls</th>
                <th>Net opened</th>
                <th>Realized P/L</th>
                <th className="l">Status</th>
              </tr>
            </thead>
            <tbody>
              {data.trades.map((t) => (
                <TradeRows
                  key={t.id}
                  t={t}
                  open={open.has(t.id)}
                  toggle={() =>
                    setOpen((s) => {
                      const n = new Set(s);
                      if (n.has(t.id)) n.delete(t.id);
                      else n.add(t.id);
                      return n;
                    })
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="dim-text">No trades yet.</p>
      )}

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

      <h3>Order history</h3>
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
                {o.intent} · {o.strategy} · {o.duration}
                {o.reason ? <div className="sub">{o.reason}</div> : null}
              </td>
              <td className="l legs-cell">
                {o.legs.map((l) => (
                  <div key={l.symbol}>{legText(l.symbol, l.quantity)}</div>
                ))}
              </td>
              <td>{net(o.limit_net)}</td>
              <td className={`l status ${o.status.toLowerCase()}`}>
                {o.status.toLowerCase()}
                {o.filled_at ? <div className="sub">{when(o.filled_at)}</div> : null}
              </td>
              <td>
                {onCancel ? (
                  <button type="button" className="small" onClick={() => onCancel(o)}>
                    Cancel
                  </button>
                ) : (
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

function TradeRows({ t, open, toggle }: { t: SimTrade; open: boolean; toggle: () => void }) {
  const opened = t.events.filter((e) => ["open", "roll", "adjust"].includes(e.kind)).reduce((a, e) => a + e.net, 0);
  return (
    <>
      <tr className="strat" onClick={toggle} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && toggle()} aria-expanded={open}>
        <td className="l name">
          <span className="caret">{open ? "▾" : "▸"}</span>
          <span className="sym-inline">{t.underlying}</span>
          {t.strategy}
        </td>
        <td>{when(t.opened_at)}</td>
        <td>{when(t.closed_at)}</td>
        <td>{t.events.filter((e) => e.kind === "roll").length}</td>
        <td>{signed(opened)}</td>
        <td className={tone(t.realized_pl)}>{t.realized_pl === null ? "–" : signed(t.realized_pl)}</td>
        <td className="l">{t.status}</td>
      </tr>
      {open
        ? t.events.map((e, i) => (
            <tr className="leg" key={i}>
              <td className="l">
                {e.kind}
                {e.order_id ? <span className="sub"> #{e.order_id}</span> : null}
              </td>
              <td>{when(e.at)}</td>
              <td colSpan={2} className="l legs-cell">
                {e.legs.map((l) => (
                  <div key={l.symbol + l.quantity}>
                    {legText(l.symbol, l.quantity)} @ {l.price.toFixed(2)}
                  </div>
                ))}
              </td>
              <td className={tone(e.net)}>{signed(e.net)}</td>
              <td className="sub">fees {money(e.commission, 2)}</td>
              <td />
            </tr>
          ))
        : null}
    </>
  );
}
