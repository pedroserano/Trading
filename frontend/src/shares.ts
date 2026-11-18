import { money, num } from "./format";
import type { StrategyView } from "./types";

/** A position of shares only (no options): bought stock, or short stock. */
export function isShares(st: StrategyView): boolean {
  return st.legs.length > 0 && st.legs.every((l) => l.kind === "EQUITY");
}

/** "300 shares @ 112.40": the count and the cost per share, weighted over every lot. */
export function sharesText(st: StrategyView, withAvg = true): string {
  const q = st.legs.reduce((a, l) => a + l.quantity, 0);
  const cost = st.legs.reduce((a, l) => a + l.quantity * l.avg_price, 0);
  const avg = q ? cost / q : null;
  const count = `${money(Math.abs(q))} share${Math.abs(q) === 1 ? "" : "s"}${q < 0 ? " short" : ""}`;
  return avg === null || !withAvg ? count : `${count} @ ${num(avg, 2)}`;
}

function add(a: number | null | undefined, b: number | null | undefined): number | null {
  if (a === null || a === undefined) return b ?? null;
  if (b === null || b === undefined) return a;
  return a + b;
}

/** Shares bought in several orders are separate trades in Schwab's history. Show them as one
 * row per underlying (long and short apart), keeping each purchase as a lot inside it. */
export function mergeShares(strategies: StrategyView[]): StrategyView[] {
  const out: StrategyView[] = [];
  const groups: Record<string, StrategyView[]> = {};
  for (const st of strategies) {
    if (!isShares(st)) {
      out.push(st);
      continue;
    }
    const side = st.legs[0].quantity < 0 ? "short" : "long";
    (groups[side] ??= []).push(st);
  }
  for (const lots of Object.values(groups)) {
    if (lots.length === 1) {
      out.push(lots[0]);
      continue;
    }
    // One leg per symbol: quantities summed, cost weighted.
    const q = lots.reduce((a, st) => a + st.legs.reduce((b, l) => b + l.quantity, 0), 0);
    const cost = lots.reduce((a, st) => a + st.legs.reduce((b, l) => b + l.quantity * l.avg_price, 0), 0);
    const first = lots[0].legs[0];
    const n = lots.map((st) => st.numbers);
    const sum = (k: "value" | "pl" | "day_pl" | "delta" | "delta_bw" | "theta" | "vega" | "gamma") =>
      n.reduce<number | null>((a, x) => add(a, x[k]), null);
    out.push({
      trade_id: null,
      rolls: 0,
      name: lots[0].name,
      defined_risk: false,
      flags: lots.flatMap((st) => st.flags),
      legs: [{ ...first, quantity: q, avg_price: q ? cost / q : first.avg_price, delta: sum("delta") }],
      numbers: {
        ...lots[0].numbers,
        open_value: n.reduce((a, x) => a + x.open_value, 0),
        value: sum("value"),
        pl: sum("pl"),
        day_pl: sum("day_pl"),
        delta: sum("delta"),
        theta: sum("theta"),
        vega: sum("vega"),
        gamma: sum("gamma"),
        delta_bw: sum("delta_bw"),
      },
      lots,
    });
  }
  return out;
}
