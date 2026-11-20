import OrderCheck from "./OrderCheck";
import { useCallback, useContext, useEffect, useState } from "react";
import { getJson, postJson } from "./api";
import { usePolling } from "./usePolling";
import { useMediaQuery } from "./useMediaQuery";
import { ChainTimeline, chainSummary, legText, when } from "./ChainHistory";
import { ALL, TicketContext, ViewContext } from "./context";
import type { TicketSpec } from "./OrderTicket";
import { money, pct, signed, tone } from "./format";
import type { Activity as ActivityData, SimOrder, SimTrade } from "./types";

function net(v: number | null): string {
  if (v === null) return "–";
  return `${Math.abs(v).toFixed(2)} ${v > 0 ? "cr" : v < 0 ? "db" : ""}`;
}

/** An order's legs in one short line: "Jan 15 '27: −660P +665P +765C −770C". */
function legsLine(legs: { symbol: string; quantity: number }[]): string {
  const byExp = new Map<string, string[]>();
  for (const l of legs) {
    const m = l.symbol.match(/^(.{1,6}?)\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/);
    const q = Math.abs(l.quantity);
    const sign = l.quantity < 0 ? "−" : "+";
    if (!m) {
      const k = "";
      byExp.set(k, [...(byExp.get(k) ?? []), `${sign}${q} sh`]);
      continue;
    }
    const exp = new Date(`20${m[2]}-${m[3]}-${m[4]}T12:00:00`).toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "2-digit",
    });
    const k = exp.replace(/, (\d{2})$/, " '$1");
    const strike = Number(m[6]) / 1000;
    byExp.set(k, [...(byExp.get(k) ?? []), `${sign}${q > 1 ? `${q}×` : ""}${strike}${m[5]}`]);
  }
  return [...byExp.entries()].map(([k, v]) => (k ? `${k}: ${v.join(" ")}` : v.join(" "))).join(" · ");
}

/** "pending activation" → "pending"; the full status stays in the tooltip. */
function shortStatus(o: SimOrder): string {
  const s = (o.schwab_status ?? o.status).toLowerCase().replace(/_/g, " ");
  return s.split(" ")[0];
}

const CHANGE = "Tap to change the price or duration and replace the order";

/** Working orders on a phone: each fits the screen, Cancel in reach without scrolling. */
function WorkingCompact({
  orders,
  onCancel,
  onOpen,
}: {
  orders: SimOrder[];
  onCancel: (o: SimOrder) => void;
  onOpen: (o: SimOrder) => void;
}) {
  return (
    <ul className="work-list">
      {orders.map((o) => (
        <li key={o.id} className="tappable" title={CHANGE} onClick={() => onOpen(o)}>
          <div className="work-line">
            <span className="sym-inline">{o.underlying}</span>
            <span className="work-what">{o.intent}</span>
            {o.account_name ? <span className="acct-tag">{o.account_name}</span> : null}
            <span className="work-limit">{net(o.limit_net)}</span>
            <span className={`work-status status ${o.status.toLowerCase()}`} title={(o.schwab_status ?? o.status).toLowerCase().replace(/_/g, " ")}>
              {shortStatus(o)}
            </span>
            <button
              type="button"
              className="small"
              onClick={(e) => {
                e.stopPropagation();
                onCancel(o);
              }}
            >
              Cancel
            </button>
          </div>
          <div className="work-legs">
            {legsLine(o.legs)} · {o.duration}
          </div>
        </li>
      ))}
    </ul>
  );
}

export default function Activity() {
  const compact = useMediaQuery("(max-width: 700px)");
  // An account, or ALL: every Schwab account, each order and trade tagged with its own.
  const account = useContext(ViewContext);
  const all = account === ALL;
  const [data, setData] = useState<ActivityData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [limit, setLimit] = useState(300);

  const load = useCallback(
    async (current: () => boolean = () => true) => {
    try {
      const d = await getJson<ActivityData>(`/api/activity?account=${account}&limit=${limit}`);
      if (!current()) return;
      setData(d);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  },
    [account, limit],
  );

  useEffect(() => setData(null), [account]);
  usePolling(load, 5000);

  // A working order on the ticket again, at its own price and duration: placing it there
  // replaces it.
  const openTicket = useContext(TicketContext);
  const change = async (o: SimOrder) => {
    const acct = o.account ?? account;
    let entry = { automanage: false, view: null as string | null, play: null as string | null };
    try {
      entry = await getJson(`/api/orders/${encodeURIComponent(String(o.id))}/entry?account=${acct}`);
    } catch {
      /* the ticket's own default */
    }
    const opens = o.legs.some((l) => l.effect === "open");
    const closes = o.legs.some((l) => l.effect === "close");
    const intent = (
      ["open", "close", "roll", "adjust"].includes(o.intent) ? o.intent : opens && closes ? "roll" : closes ? "close" : "open"
    ) as TicketSpec["intent"];
    const verb = { open: "Open", close: "Close", roll: "Roll", adjust: "Adjust" }[intent];
    openTicket({
      title: `Replace #${o.id}: ${verb} ${o.underlying} ${o.strategy ?? ""}`.trim(),
      account: acct,
      intent,
      underlying: o.underlying,
      tradeId: o.trade_id,
      legs: o.legs.map((l) => ({ symbol: l.symbol, quantity: l.quantity, effect: l.effect })),
      replaces: o.id,
      limit: o.limit_net,
      duration: o.duration,
      automanage: entry.automanage,
      view: entry.view,
      play: entry.play,
      onClosed: (placed) => {
        if (placed) load();
      },
    });
  };

  const cancel = async (o: SimOrder) => {
    try {
      const acct = o.account ?? account;
      if (acct !== "sim" && !window.confirm(`Cancel order #${o.id} at Schwab?`)) return;
      await postJson(`/api/orders/${o.id}/cancel?account=${acct}`, {});
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!data) return error ? <div className="banner warn">{error}</div> : <div className="empty">Loading…</div>;
  const live = data.account !== "sim";
  const working = data.orders.filter((o) => o.status === "WORKING");
  const done = data.orders.filter((o) => o.status !== "WORKING").slice(0, 50);
  const realized = data.trades.reduce((a, t) => a + (t.realized_pl ?? 0), 0);

  return (
    <div className="activity">
      {error ? <div className="banner warn">{error}</div> : null}
      {live ? (
        <div className="sim-summary">
          <span className="badge live">{all ? "ALL SCHWAB ACCOUNTS" : "SCHWAB ACCOUNT"}</span>
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
        </div>
      )}

      {live && data.error ? <div className="msg warn">History sync: {data.error}</div> : null}
      {live && !all && data.live ? (
        <div className={`msg ${data.live.on ? "warn" : ""}`}>
          Live trading is <b>{data.live.on ? "on" : "off"}</b>
          {data.live.on
            ? `: ${data.live.all_symbols ? "any underlying" : data.live.symbols.length ? data.live.symbols.join(", ") : "no underlyings allowed"}, up to ${data.live.max_contracts} contract${data.live.max_contracts > 1 ? "s" : ""} per leg.`
            : ": LIVE_TRADING=true is not set in the server's .env file."}{" "}
          <a href="#/settings/live">Schwab limits</a>
          {data.live.error ? <span className="sub"> Reading orders from Schwab: {data.live.error}</span> : null}
        </div>
      ) : null}
      <h3>{live ? "Working orders at Schwab" : "Working orders"}</h3>
      {working.length ? (
        compact ? (
          <WorkingCompact orders={working} onCancel={cancel} onOpen={change} />
        ) : (
          <OrderTable orders={working} onCancel={cancel} onOpen={change} />
        )
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
          <table className="positions fit oneline">
            <thead>
              <tr>
                <th className="l">Trade</th>
                <th>Net so far</th>
                <th>Realized P/L</th>
                <th className="l">Status</th>
                <th>Opened</th>
                <th>Closed</th>
                <th>Rolls</th>
              </tr>
            </thead>
            <tbody>
              {data.trades.map((t) => (
                <TradeRows
                  key={String(t.id)}
                  t={t}
                  account={t.account ?? account}
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
          <table className="positions fit oneline">
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

      {live && !all ? <OrderCheck /> : null}

      <h3>{live ? "Orders at Schwab, last 7 days" : "Order history"}</h3>
      {done.length ? <OrderTable orders={done} /> : <p className="dim-text">No orders yet.</p>}
    </div>
  );
}

function OrderTable({
  orders,
  onCancel,
  onOpen,
}: {
  orders: SimOrder[];
  onCancel?: (o: SimOrder) => void;
  onOpen?: (o: SimOrder) => void;
}) {
  return (
    <div className="table-wrap">
      <table className="positions orders fit oneline ph-h1">
        <thead>
          <tr>
            <th className="l">#</th>
            <th className="l">Order</th>
            <th>Limit</th>
            <th className="l">Status</th>
            <th />
            <th className="l">Legs</th>
            <th className="l">Placed</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <tr
              key={o.id}
              className={onOpen ? "tappable" : undefined}
              title={onOpen ? CHANGE : undefined}
              onClick={onOpen ? () => onOpen(o) : undefined}
            >
              <td className="l">{o.id}</td>
              <td className="l">
                <span className="sym-inline">{o.underlying}</span>
                {[o.intent, o.strategy, o.duration].filter(Boolean).join(" · ")}
                {o.account_name ? <span className="acct-tag">{o.account_name}</span> : null}
                {o.placed_by_key ? (
                  <span className="badge ai" title="Placed through the app's API with this key (Settings, Connection, AI access)">
                    AI key: {o.placed_by_key}
                  </span>
                ) : o.placed_by_app ? (
                  <span className="sub"> · placed by the app</span>
                ) : null}
                {o.reason ? <div className="sub reason">{o.reason}</div> : null}
              </td>
              <td>{net(o.limit_net)}</td>
              <td className={`l status ${o.status.toLowerCase()}`}>
                {(o.schwab_status ?? o.status).toLowerCase().replace(/_/g, " ")}
                {o.filled_at ? <div className="sub">{when(o.filled_at)}</div> : null}
              </td>
              <td>
                {onCancel ? (
                  <button
                    type="button"
                    className="small"
                    onClick={(e) => {
                      e.stopPropagation();
                      onCancel(o);
                    }}
                  >
                    Cancel
                  </button>
                ) : o.schwab_json === undefined ? null : (
                  <details className="json inline">
                    <summary>JSON</summary>
                    <pre>{JSON.stringify(o.schwab_json, null, 2)}</pre>
                  </details>
                )}
              </td>
              <td className="l legs-cell">
                {o.legs.map((l) => (
                  <div key={l.symbol}>{legText(l.symbol, l.quantity)}</div>
                ))}
              </td>
              <td className="l">{when(o.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TradeRows(props: { t: SimTrade; account: string; open: boolean; toggle: () => void; onChanged: () => void }) {
  const { t, open, toggle } = props;
  const sum = chainSummary(t);
  return (
    <>
      <tr className="strat" onClick={toggle} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && toggle()} aria-expanded={open}>
        <td className="l name">
          <span className="caret">{open ? "▾" : "▸"}</span>
          <span className="sym-inline">{t.underlying}</span>
          {t.strategy}
          {t.account_name ? <span className="acct-tag">{t.account_name}</span> : null}
          {t.carried_in ? (
            <span className="sub" title="Held before the transaction history starts">
              {" "}
              carried in
            </span>
          ) : null}
        </td>
        <td className={tone(sum.net)}>{signed(sum.net)}</td>
        <td className={tone(t.realized_pl)}>{t.realized_pl === null ? "" : signed(t.realized_pl)}</td>
        <td className="l">{t.status}</td>
        <td>{t.carried_in ? `before ${when(t.opened_at)}` : when(t.opened_at)}</td>
        <td>{when(t.closed_at)}</td>
        <td>{sum.rolls}</td>
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
