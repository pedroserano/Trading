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
export function ChainTimeline({ chain, account, onChanged }: { chain: SimTrade; account: string; onChanged?: () => void }) {
  const [error, setError] = useState<string | null>(null);
  let running = 0;
  const split = async (group: string) => {
    try {
      await postJson(`/api/chains/split?account=${encodeURIComponent(account)}`, { group });
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
                  {account !== "sim" && e.group && (e.auto_linked || e.note === "Linked by you") ? (
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

interface Covered {
  side: "long" | "short";
  shares: number;
  avg_price: number;
  premium: number;
  per_share: number;
  adjusted: number;
  since: number | null;
  chains: { id: string | number; strategy: string; opened_at: number; closed_at: number | null; status: string; net: number }[];
}

/** Shares' history: what covered calls (covered puts, on short shares) have done to the cost
 * basis, and each of them, one tap from its own history. */
function CoveredBasis({ c, onOpen }: { c: Covered; onOpen: (id: string | number) => void }) {
  const short = c.side === "short";
  const what = short ? "Covered puts" : "Covered calls";
  // Lower is better for long shares; higher is better for short ones.
  const good = short ? c.adjusted >= c.avg_price : c.adjusted <= c.avg_price;
  return (
    <div className="covered">
      <div className="ticket-sum">
        <span>
          <span className="lbl">{short ? "Sold at" : "Average paid"}</span> {c.avg_price.toFixed(2)}
        </span>
        <span>
          <span className="lbl">{what}</span> <span className={tone(c.premium)}>{signed(c.premium)}</span>{" "}
          <span className="sub">({c.chains.length})</span>
        </span>
        <span>
          <span className="lbl">{short ? "Effective sale" : "Adjusted basis"}</span>{" "}
          <b className={c.chains.length ? (good ? "pos" : "neg") : ""}>{c.adjusted.toFixed(2)}</b>{" "}
          <span className="sub">
            ({signed(short ? c.per_share : -c.per_share, 2)} a share)
          </span>
        </span>
      </div>
      {c.chains.length ? (
        <ul className="covered-list">
          {c.chains.map((x) => (
            <li key={String(x.id)}>
              <button type="button" className="link" onClick={() => onOpen(x.id)}>
                <span>{x.strategy}</span>
                <span className="dim-text">
                  {when(x.opened_at)}
                  {x.status === "open" ? " · open" : ""}
                </span>
                <span className={tone(x.net)}>{signed(x.net)}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="dim-text">
          No {what.toLowerCase()} written against these shares yet. Premium from them lowers the basis as each is sold; a buyback or roll for a debit raises it again.
        </p>
      )}
    </div>
  );
}

export default function ChainHistory({
  account,
  chainId,
  onClose,
  shares = false,
}: {
  account: string;
  chainId: string | number;
  onClose: () => void;
  /** Opened from a shares row: also show what covered calls have done to their basis. The
   * shares' chain can carry any name (an assigned put's chain keeps the put's). */
  shares?: boolean;
}) {
  // The trail of histories opened from here: the shares, then a covered call, and back.
  const [trail, setTrail] = useState<(string | number)[]>([chainId]);
  const id = trail[trail.length - 1];
  const [chain, setChain] = useState<SimTrade | null>(null);
  const [covered, setCovered] = useState<Covered | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      const c = await getJson<SimTrade>(`/api/chains/${encodeURIComponent(String(id))}?account=${account}`);
      setChain(c);
      setCovered(null);
      if (shares && trail.length === 1 && c.status === "open") {
        try {
          setCovered(await getJson<Covered>(`/api/covered?account=${account}&symbol=${encodeURIComponent(c.underlying)}`));
        } catch {
          /* no covered-call figures: the timeline still shows */
        }
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };
  useEffect(() => {
    setChain(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [account, id]);

  const sum = chain ? chainSummary(chain) : null;
  return (
    <div className="ticket-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="ticket history" role="dialog" aria-modal="true" aria-label="Trade history" tabIndex={-1} onKeyDown={(e) => e.key === "Escape" && onClose()}>
        <div className="ticket-head">
          {trail.length > 1 ? (
            <button type="button" className="link back" onClick={() => setTrail(trail.slice(0, -1))}>
              ‹ Shares
            </button>
          ) : null}
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
            {covered ? <CoveredBasis c={covered} onOpen={(x) => setTrail([...trail, x])} /> : null}
            <ChainTimeline chain={chain} account={account} onChanged={load} />
          </>
        ) : null}
      </div>
    </div>
  );
}
