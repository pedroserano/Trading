import { useEffect, useState } from "react";
import { getJson } from "./api";
import { clock } from "./format";

type Item = {
  label: string;
  symbol: string;
  contract: string;
  price: number | null;
  change: number | null;
  change_pct: number | null;
  fallback: boolean;
};

/** Decimals as each market is quoted: currencies to 4 or 6, everything else to 2. */
function priceText(i: Item): string {
  if (i.price === null) return "–";
  const d = i.price < 0.1 ? 6 : i.price < 10 ? 4 : 2;
  return i.price.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

/** One line of the major futures: price and the day's move, like a tastylive market rundown. */
export default function MarketStrip() {
  const [items, setItems] = useState<Item[] | null>(null);
  const [at, setAt] = useState<number | null>(null);

  useEffect(() => {
    let stop = false;
    const load = async () => {
      try {
        const r = await getJson<{ items: Item[]; at: number | null }>("/api/market");
        if (!stop) {
          setItems(r.items);
          setAt(r.at);
        }
      } catch {
        /* the next poll retries; the strip keeps its last values */
      }
    };
    load();
    const t = window.setInterval(load, 15000);
    return () => {
      stop = true;
      window.clearInterval(t);
    };
  }, []);

  if (!items || !items.length) return null;
  return (
    <div className="market-strip" aria-label="Markets">
      {items.map((i) => {
        const tone = i.change_pct === null ? "" : i.change_pct > 0 ? "pos" : i.change_pct < 0 ? "neg" : "";
        const title =
          `${i.label}: ${i.fallback ? `${i.symbol} (standing in: the future could not be quoted)` : i.contract}` +
          (i.change !== null ? `, ${i.change > 0 ? "+" : ""}${i.change} on the day` : "") +
          (at ? `. As of ${clock(at)}.` : "");
        return (
          <span key={i.label} className="mkt" title={title}>
            <span className="mkt-label">
              {i.label}
              {i.fallback ? "*" : ""}
            </span>
            <span className="mkt-price">{priceText(i)}</span>
            <span className={`mkt-chg ${tone}`}>
              {i.change_pct === null ? "" : `${i.change_pct > 0 ? "+" : ""}${i.change_pct.toFixed(2)}%`}
            </span>
          </span>
        );
      })}
    </div>
  );
}
