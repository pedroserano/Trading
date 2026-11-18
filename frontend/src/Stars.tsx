import { num, shortDate } from "./format";

export interface Liquidity {
  stars: number | null;
  spread_pct: number | null;
  width: number | null;
  oi: number | null;
}

/** What a liquidity rating is made of, for its tooltip. */
export function liquidityTitle(l: Liquidity | null | undefined, expiration?: string | null): string {
  if (!l || l.stars === null) return "Liquidity: no options market data yet";
  const parts = [];
  if (l.width !== null) parts.push(`${num(l.width, 2)} wide${l.spread_pct !== null ? ` (${(l.spread_pct * 100).toFixed(1)}%)` : ""}`);
  if (l.oi !== null) parts.push(`open interest ${l.oi.toLocaleString("en-US")}`);
  return (
    `Liquidity ${l.stars} of 5. At the money${expiration ? `, ${shortDate(expiration)}` : ""}: ${parts.join(", ")}. ` +
    "Stars for a tight market (a nickel wide or 1% of the price: 2, up to 3%: 1) and deep open interest " +
    "(10,000+: 2, 1,000+: 1), after tastylive."
  );
}

/** 1 to 5 stars, filled and empty. */
export default function Stars({ n, title }: { n: number | null; title?: string }) {
  if (n === null) return <span className="dim-text" title={title}>–</span>;
  return (
    <span className="stars" title={title} aria-label={`${n} of 5 stars`}>
      {"★★★★★".slice(0, n)}
      <span className="stars-off">{"★★★★★".slice(n)}</span>
    </span>
  );
}
