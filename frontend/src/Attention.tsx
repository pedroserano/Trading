import { useContext } from "react";
import { AutomationContext } from "./Automation";
import { shortDate } from "./format";
import type { Snapshot } from "./types";

/** What on Positions wants a decision, worst first: losses, tested shorts, early
 * assignment risk, 21 DTE, profit targets, then reports and proposals. */
const ORDER: Record<string, number> = { loss: 0, tested: 1, dividend: 2, dte: 3, profit: 4, data: 6 };
const TONE: Record<string, string> = {
  loss: "bad",
  tested: "warn",
  dividend: "warn",
  dte: "info",
  profit: "ok",
  data: "dim",
};
const WHAT: Record<string, string> = {
  loss: "Loss limit",
  tested: "Tested",
  dividend: "Early assignment risk",
  dte: "Time to manage",
  profit: "Take profit",
  data: "No live quote",
};

export interface Item {
  key: string;
  rank: number;
  tone: string;
  what: string;
  title: string;
  detail: string;
  /** The position row to open, as data-trade on its row: "SYMBOL|name|expiration". */
  trade?: string;
  /** Or an element to scroll to. */
  target?: string;
}

/** Earnings within this many sessions of the last one to be out show here. */
const EARNINGS_SESSIONS = 5;

export function attentionItems(s: Snapshot, proposals: number, blocked: string | null): Item[] {
  const out: Item[] = [];
  for (const u of s.underlyings) {
    for (const st of u.strategies) {
      const trade = `${u.symbol}|${st.name}|${st.numbers.expiration ?? ""}`;
      for (const f of st.flags) {
        out.push({
          key: `${trade}|${f.kind}`,
          rank: ORDER[f.kind] ?? 5,
          tone: TONE[f.kind] ?? "info",
          what: WHAT[f.kind] ?? f.label,
          title: `${u.symbol} ${st.name}${st.numbers.expiration ? ` ${shortDate(st.numbers.expiration)}` : ""}`,
          detail: f.detail,
          trade,
        });
      }
    }
    const e = u.earnings;
    if (e && e.days >= 0 && e.sessions_left <= EARNINGS_SESSIONS) {
      out.push({
        key: `${u.symbol}|earnings`,
        rank: 5,
        tone: "info",
        what: "Earnings",
        title: `${u.symbol} reports ${shortDate(e.date)}${e.time === "bmo" ? " before the open" : e.time === "amc" ? " after the close" : ""}`,
        detail:
          e.sessions_left <= 0
            ? "Today is the last session to be out before the report"
            : `Last session to be out: ${shortDate(e.exit_by)} (${e.sessions_left} session${e.sessions_left === 1 ? "" : "s"})`,
        trade: `${u.symbol}|`,
      });
    }
  }
  if (proposals) {
    out.push({
      key: "proposals",
      rank: -1,
      tone: "warn",
      what: "Waiting for you",
      title: `${proposals} automanage proposal${proposals === 1 ? "" : "s"}`,
      detail: "Place or dismiss below",
      target: "proposals",
    });
  }
  if (blocked) {
    out.push({ key: "blocked", rank: -2, tone: "warn", what: "Automation", title: "Holding", detail: blocked });
  }
  return out.sort((a, b) => a.rank - b.rank);
}

/** The top of Positions: only what wants a decision, in plain words. Tapping an item opens
 * that position below. */
export default function Attention({ s }: { s: Snapshot }) {
  const { view } = useContext(AutomationContext);
  const items = attentionItems(s, view?.proposals.length ?? 0, view && !view.paused ? (view.blocked ?? null) : null);
  const go = (it: Item) => {
    if (it.trade) window.dispatchEvent(new CustomEvent("focus-trade", { detail: it.trade }));
    else if (it.target) document.getElementById(it.target)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  if (!s.underlyings.length && !items.length) return null;
  if (!items.length) {
    return (
      <div className="attention calm">
        <span className="attention-dot ok" aria-hidden="true" /> Nothing needs you right now.
      </div>
    );
  }
  return (
    <section className="attention" aria-label="Needs attention">
      <h3>
        Needs attention <span className="sub">{items.length}</span>
      </h3>
      <ul>
        {items.map((it) => (
          <li key={it.key}>
            <button type="button" className={`attention-item ${it.tone}`} onClick={() => go(it)}>
              <span className="attention-what">{it.what}</span>
              <span className="attention-title">{it.title}</span>
              <span className="attention-detail">{it.detail}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
