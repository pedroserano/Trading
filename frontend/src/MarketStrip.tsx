import { lazy, Suspense, useCallback, useState } from "react";
import { getJson } from "./api";
import { usePolling } from "./usePolling";
import { clock } from "./format";

const PriceChart = lazy(() => import("./PriceChart"));

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

/** Shown while the strip is folded: the market as a whole and its fear gauge. */
const PINNED = ["S&P", "Nasdaq", "VIX"];

function remembered(): boolean {
  try {
    return localStorage.getItem("markets.open") === "1";
  } catch {
    return false;
  }
}

/** One line of the major futures: price and the day's move, like a tastylive market rundown.
 * Folded to the S&P, Nasdaq and VIX until opened; the choice is remembered. */
export default function MarketStrip() {
  const [items, setItems] = useState<Item[] | null>(null);
  const [at, setAt] = useState<number | null>(null);
  const [open, setOpenState] = useState(remembered);
  const [chartOf, setChartOf] = useState<string | null>(null);
  const setOpen = (v: boolean) => {
    setOpenState(v);
    try {
      localStorage.setItem("markets.open", v ? "1" : "0");
    } catch {
      /* private mode: not remembered */
    }
  };

  const loadMarket = useCallback(async (current: () => boolean) => {
    try {
      const r = await getJson<{ items: Item[]; at: number | null }>("/api/market");
      if (current()) {
        setItems(r.items);
        setAt(r.at);
      }
    } catch {
      /* the next poll retries; the strip keeps its last values */
    }
  }, []);
  usePolling(loadMarket, 15000);

  if (!items || !items.length) return null;
  const pinned = items.filter((i) => PINNED.includes(i.label));
  const shown = open || !pinned.length ? items : pinned;
  const charted = chartOf ? items.find((i) => i.label === chartOf) : undefined;
  return (
    <>
    <div className={`market-strip ${open ? "open" : "folded"}`} aria-label="Markets">
      {shown.map((i) => {
        const tone = i.change_pct === null ? "" : i.change_pct > 0 ? "pos" : i.change_pct < 0 ? "neg" : "";
        const title =
          `${i.label}: ${i.fallback ? `${i.symbol} (standing in: the future could not be quoted)` : i.contract}` +
          (i.change !== null ? `, ${i.change > 0 ? "+" : ""}${i.change} on the day` : "") +
          (at ? `. As of ${clock(at)}.` : "");
        return (
          <button
            type="button"
            key={i.label}
            className={`mkt ${chartOf === i.label ? "charted" : ""}`}
            title={`${title.replace(/\.$/, "")}. Tap for a chart.`}
            aria-pressed={chartOf === i.label}
            onClick={() => setChartOf(chartOf === i.label ? null : i.label)}
          >
            <span className="mkt-label">
              {i.label}
              {i.fallback ? "*" : ""}
            </span>
            <span className="mkt-price">{priceText(i)}</span>
            <span className={`mkt-chg ${tone}`}>
              {i.change_pct === null ? "" : `${i.change_pct > 0 ? "+" : ""}${i.change_pct.toFixed(2)}%`}
            </span>
          </button>
        );
      })}
      {pinned.length ? (
        <button
          type="button"
          className="mkt mkt-more"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          title={open ? "Show only the S&P, Nasdaq and VIX" : "Show every market"}
        >
          {open ? "Fewer ▴" : `${items.length - pinned.length} more ▾`}
        </button>
      ) : null}
    </div>
    {charted ? (
      <section className="market-chart">
        <div className="market-chart-head">
          <strong>{charted.label}</strong>
          <span className="dim-text">
            {chartSource(charted)}
          </span>
          <button type="button" className="link" onClick={() => setChartOf(null)} aria-label="Close the chart">
            ×
          </button>
        </div>
        <Suspense fallback={<div className="dim-text">Loading chart…</div>}>
          <PriceChart
            key={charted.label}
            symbol={charted.label}
            spot={null}
            expiration={null}
            expectedMove={null}
            url={`/api/market/chart?label=${encodeURIComponent(charted.label)}`}
            plain
          />
        </Suspense>
      </section>
    ) : null}
    </>
  );
}

/** What the chart shows: futures roll from contract to contract, so it is the index, or the
 * fund that stands in for the future, over a year of daily bars. */
function chartSource(i: Item): string {
  const by: Record<string, string> = {
    "S&P": "S&P 500 index ($SPX)",
    Nasdaq: "Nasdaq Composite ($COMPX)",
    Russell: "Russell 2000 index ($RUT)",
    Dow: "Dow Jones Industrial Average ($DJI)",
    VIX: "Cboe Volatility Index ($VIX)",
    Crude: "USO, the oil fund, standing in for crude futures",
    Gold: "GLD, the gold fund, standing in for gold futures",
    Silver: "SLV, the silver fund, standing in for silver futures",
    "30-yr": "TLT, the 20+ year Treasury fund, standing in for bond futures",
    "10-yr": "IEF, the 7-10 year Treasury fund, standing in for note futures",
    Euro: "FXE, the euro fund, standing in for euro futures",
    Yen: "FXY, the yen fund, standing in for yen futures",
    Bitcoin: "IBIT, the bitcoin fund, standing in for bitcoin futures",
  };
  return by[i.label] ?? i.symbol;
}
