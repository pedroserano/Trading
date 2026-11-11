import { useEffect, useState } from "react";
import { getJson, postJson } from "./api";
import { money, shortDate, signed, strike, tone } from "./format";
import type { SimTrade } from "./types";

export function when(ts: number | null): string {
  if (!ts) return "–";
  return new Date(ts * 1000).toLocaleString("en-US", { month: "short", day: "numeric", year: "2-digit", hour: "numeric", minute: "2-digit" });
}

export function legText(symbol: string, quantity: number): string {
  // "SPY   261218P00555000" -> "-1 Dec 18 555 P"
  const m = symbol.match(/^(.{1,6}?)\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/);
  if (!m) return `${signed(quantity)} ${symbol.trim()} shares`;
  const exp = `20${m[2]}-${m[3]}-${m[4]}`;
  return `${signed(quantity)} ${shortDate(exp)} ${strike(Number(m[6]) / 1000)} ${m[5]}`;
}

const KIND: Record<string, string> = {
  open: "Opened",
  roll: "Rolled",
  adjust: "Adjusted",
  close: "Closed",
  expire: "Expired",
  settle: "Settled",
  assign: "Assigned",
  exercise: "Exercised",
  carried: "Carried in",
};

/** The roll history of one trade chain, oldest first, with a running total. */
export function ChainTimeline({ chain, account, onChanged }: { chain: SimTrade; account: "live" | "sim"; onChanged?: () => void }) {
  const [error, setError] = useState<string | null>(null);
  let running = 0;
  const split = async (group: string) => {
    try {
      await postJson("/api/chains/split", { group });
      onChanged?.();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <>
      {error ? <div className="msg bad">{error}</div> : null}
      <ol className="timeline">
        {chain.events.map((e, i) => {
          running += e.net - e.commission;
          return (
            <li key={i} className={`ev ${e.kind}`}>
              <div className="ev-head">
                <span className="ev-kind">{KIND[e.kind] ?? e.kind}</span>
                <span className="dim-text">{when(e.at)}</span>
                <span className={`ev-net ${tone(e.net)}`}>
                  {signed(e.net)} <span className="sub">{e.net >= 0 ? "credit" : "debit"}</span>
                </span>
                <span className="sub">fees {money(e.commission, 2)}</span>
                <span className="ev-run">
                  Running <b className={tone(running)}>{signed(running)}</b>
                </span>
              </div>
              <div className="ev-legs">
                {e.legs.map((l, j) => (
                  <span key={j}>
                    {legText(l.symbol, l.quantity)} @ {l.price.toFixed(2)}
                  </span>
                ))}
              </div>
              {e.note ? (
                <div className="ev-note">
                  {e.note}
                  {account === "live" && e.group && (e.auto_linked || e.note === "Linked by you") ? (
                    <button type="button" className="small" onClick={() => split(String(e.group))}>
                      Split into its own trade
                    </button>
                  ) : null}
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
    </>
  );
}

export function chainSummary(c: SimTrade) {
  const net = c.events.reduce((a, e) => a + e.net, 0);
  const fees = c.events.reduce((a, e) => a + e.commission, 0);
  const rolls = c.events.filter((e) => e.kind === "roll").length;
  const days = ((c.closed_at ?? Date.now() / 1000) - c.opened_at) / 86400;
  return { net, fees, rolls, days };
}

export default function ChainHistory({ account, chainId, onClose }: { account: "live" | "sim"; chainId: string | number; onClose: () => void }) {
  const [chain, setChain] = useState<SimTrade | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      setChain(await getJson<SimTrade>(`/api/chains/${encodeURIComponent(String(chainId))}?account=${account}`));
    } catch (e) {
      setError((e as Error).message);
    }
  };
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [account, chainId]);

  const sum = chain ? chainSummary(chain) : null;
  return (
    <div className="ticket-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="ticket history" role="dialog" aria-modal="true" aria-label="Trade history" tabIndex={-1} onKeyDown={(e) => e.key === "Escape" && onClose()}>
        <div className="ticket-head">
          <strong>{chain ? `${chain.underlying} ${chain.strategy}` : "Trade history"}</strong>
          <span className={`badge ${account === "sim" ? "sim" : "live"}`}>{account === "sim" ? "SIMULATED" : "SCHWAB"}</span>
          <button type="button" className="x" aria-label="Close" onClick={onClose}>
            ×
          </button>
        </div>
        {error ? <div className="msg warn">{error}</div> : null}
        {chain && sum ? (
          <>
            <div className="ticket-sum">
              <span>
                <span className="lbl">Opened</span> {chain.carried_in ? `before ${when(chain.opened_at)}` : when(chain.opened_at)}
              </span>
              <span>
                <span className="lbl">{chain.status === "open" ? "Days in trade" : "Days held"}</span>{" "}
                {chain.carried_in ? "≥ " : ""}
                {Math.max(0, Math.round(sum.days))}
              </span>
              <span>
                <span className="lbl">Rolls</span> {sum.rolls}
              </span>
              <span>
                <span className="lbl">Net so far</span> <span className={tone(sum.net)}>{signed(sum.net)}</span>
              </span>
              <span>
                <span className="lbl">Fees</span> {money(sum.fees, 2)}
              </span>
              {chain.realized_pl !== null ? (
                <span>
                  <span className="lbl">Realized</span> <span className={tone(chain.realized_pl)}>{signed(chain.realized_pl)}</span>
                </span>
              ) : null}
            </div>
            {chain.status === "open" ? (
              <p className="dim-text">
                Net so far is every credit and debit in the chain, including rolls. Open P/L on the Positions screen adds the current value of the legs still held.
              </p>
            ) : null}
            <ChainTimeline chain={chain} account={account} onChanged={load} />
          </>
        ) : null}
      </div>
    </div>
  );
}
