import { useContext, useState } from "react";
import { AutoPanel } from "./Automation";
import ChainHistory from "./ChainHistory";
import { AccountContext, TicketContext } from "./context";
import { signed, tone } from "./format";
import { actionsFor, closeSpec, closeStockSpec, rollSpec } from "./OrderTicket";
import { sharesText } from "./shares";
import type { StrategyView } from "./types";

export function Flags({ st }: { st: StrategyView }) {
  return (
    <span className="flags">
      {st.flags.map((f, i) => (
        <span key={`${f.kind}-${i}`} className={`flag ${f.kind}`} title={f.detail}>
          {f.label}
        </span>
      ))}
    </span>
  );
}

/** One purchase of shares inside a merged Shares row. */
export function lotText(lot: StrategyView): string {
  return sharesText(lot);
}

/** The buttons under an opened position: close, roll, its history, automanage. */
export function Actions({ st, under }: { st: StrategyView; under: string | undefined }) {
  const open = useContext(TicketContext);
  const account = useContext(AccountContext);
  const [history, setHistory] = useState<StrategyView | null>(null);
  if (!under) return null;
  const acts = actionsFor(st);
  const options = st.legs.some((l) => l.kind === "OPTION");
  const shares = st.legs.some((l) => l.kind === "EQUITY");
  // A merged Shares row: one History per purchase.
  const histories = st.lots ?? (st.trade_id !== null ? [st] : []);
  return (
    <>
      {shares && account !== "sim" ? (
        <button type="button" className="small" onClick={() => open(closeStockSpec(st, under, account))}>
          Close shares
        </button>
      ) : null}
      {acts.close.map((side) => (
        <button key={`c${side}`} type="button" className="small" onClick={() => open(closeSpec(st, under, account, side))}>
          Close{side === "P" ? " puts" : side === "C" ? " calls" : ""}
        </button>
      ))}
      {acts.roll.map((side) => (
        <button key={`r${side}`} type="button" className="small" onClick={() => open(rollSpec(st, under, account, side))}>
          Roll{side === "P" ? " puts" : side === "C" ? " calls" : ""}
        </button>
      ))}
      {options && acts.roll.length === 0 ? (
        <span className="sub" title="Schwab accepts at most four legs in one order">
          No roll: it would need more than four legs.{" "}
        </span>
      ) : null}
      {histories
        .filter((h) => h.trade_id !== null)
        .map((h) => (
          <button key={String(h.trade_id)} type="button" className="small" onClick={() => setHistory(h)}>
            History
            {st.lots ? ` · ${lotText(h)}` : h.rolls ? ` · ${h.rolls} roll${h.rolls > 1 ? "s" : ""}` : ""}
          </button>
        ))}
      {history && history.trade_id !== null ? (
        <ChainHistory
          account={account}
          chainId={history.trade_id}
          onClose={() => setHistory(null)}
          shares={shares}
        />
      ) : null}
      {st.lots ? null : <AutoPanel st={st} />}
    </>
  );
}

/** A lot's figures, for the opened Shares row. */
export function LotLine({ lot }: { lot: StrategyView }) {
  const n = lot.numbers;
  return (
    <>
      {lotText(lot)}
      <span className="sub"> · P/L </span>
      <span className={tone(n.pl)}>{signed(n.pl)}</span>
      {n.day_pl ? <span className="sub"> · day {signed(n.day_pl)}</span> : null}
    </>
  );
}
