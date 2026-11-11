import { useContext, useEffect, useRef, useState } from "react";
import {
  createChart,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
} from "lightweight-charts";
import { getJson } from "./api";
import { AccountContext } from "./context";
import type { EarningsInfo } from "./types";

type Candle = { date: string; open: number; high: number; low: number; close: number };
type ChartData = {
  candles: Candle[];
  strikes: { strike: number; put_call: "P" | "C"; quantity: number; expiration: string | null }[];
  earnings: EarningsInfo | null;
  high52: number | null;
  low52: number | null;
};

const RANGES: [string, number][] = [
  ["1M", 31],
  ["3M", 92],
  ["6M", 183],
  ["1Y", 366],
];

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function iso(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function addDays(isoDate: string, n: number): string {
  const d = new Date(`${isoDate}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return iso(d);
}

/** Weekdays after `from` up to and including `to`. */
function weekdays(from: string, to: string): string[] {
  const out: string[] = [];
  for (let d = addDays(from, 1); d <= to; d = addDays(d, 1)) {
    const wd = new Date(`${d}T12:00:00Z`).getUTCDay();
    if (wd !== 0 && wd !== 6) out.push(d);
  }
  return out;
}

/**
 * Daily candles for a symbol, with the account's strikes, the 52-week range, the expected
 * move to the chosen expiration as a cone, and the next earnings report.
 */
export default function PriceChart(props: {
  symbol: string;
  spot: number | null;
  expiration: string | null;
  expectedMove: number | null;
}) {
  const account = useContext(AccountContext);
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const [data, setData] = useState<ChartData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [range, setRange] = useState(183);

  useEffect(() => {
    let stop = false;
    setData(null);
    setError(null);
    getJson<ChartData>(`/api/trade/${encodeURIComponent(props.symbol)}/chart?account=${account}`).then(
      (d) => !stop && setData(d),
      (e) => !stop && setError(String(e.message)),
    );
    return () => {
      stop = true;
    };
  }, [props.symbol, account]);

  // Build the chart whenever its data or overlays change.
  useEffect(() => {
    const el = box.current;
    if (!el || !data || !data.candles.length) return;
    const text = css("--dim") || "#8b949e";
    const line = css("--line") || "#262d37";
    const up = css("--pos") || "#56d364";
    const down = css("--neg") || "#ff7b72";
    const accent = css("--accent") || "#58a6ff";
    const warn = css("--warn") || "#d29922";
    const c = createChart(el, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: text, fontSize: 11 },
      grid: { vertLines: { color: line }, horzLines: { color: line } },
      rightPriceScale: { borderColor: line },
      timeScale: { borderColor: line, rightOffset: 2 },
      crosshair: { mode: 0 },
    });
    chart.current = c;
    const s = c.addCandlestickSeries({
      upColor: up,
      downColor: down,
      borderUpColor: up,
      borderDownColor: down,
      wickUpColor: up,
      wickDownColor: down,
      priceLineVisible: false,
    });
    candles.current = s;
    const bars = data.candles.map((b) => ({ time: b.date as Time, open: b.open, high: b.high, low: b.low, close: b.close }));
    const last = data.candles[data.candles.length - 1];
    const today = iso(new Date());
    if (props.spot && last.date < today) {
      bars.push({ time: today as Time, open: last.close, high: Math.max(last.close, props.spot), low: Math.min(last.close, props.spot), close: props.spot });
    }
    const lastDate = bars[bars.length - 1].time as string;
    s.setData(bars);
    // Days ahead, on an invisible series of their own (the candles' last bar must stay the
    // last point so the live price can update it): room for the cone and the earnings flag.
    const until = [props.expiration, data.earnings?.date].filter(Boolean).sort().pop() as string | undefined;
    // A week past the last date, so labels there are not cut off at the edge.
    const ahead = until && until > lastDate ? weekdays(lastDate, addDays(until, 7)) : [];
    const spot = props.spot ?? last.close;
    const future = c.addLineSeries({
      color: "rgba(0,0,0,0)",
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    future.setData([lastDate, ...ahead].map((d) => ({ time: d as Time, value: spot })));

    // Strikes of the account's options here: shorts solid, longs dashed.
    const seen = new Set<string>();
    for (const k of data.strikes) {
      const key = `${k.strike}${k.put_call}${Math.sign(k.quantity)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      s.createPriceLine({
        price: k.strike,
        color: k.quantity < 0 ? warn : text,
        lineWidth: 1,
        lineStyle: k.quantity < 0 ? LineStyle.Solid : LineStyle.Dashed,
        axisLabelVisible: true,
        title: `${k.strike}${k.put_call} ${k.quantity > 0 ? "+" : ""}${k.quantity}`,
      });
    }
    for (const [price, title] of [
      [data.high52, "52w high"],
      [data.low52, "52w low"],
    ] as const) {
      if (price !== null) {
        s.createPriceLine({ price, color: text, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title });
      }
    }

    // The expected move to the chosen expiration, widening with the square root of time.
    if (props.expiration && props.expectedMove && ahead.length) {
      const span = (Date.parse(props.expiration) - Date.parse(lastDate)) / 86_400_000;
      const pts = [lastDate, ...ahead.filter((d) => d <= (props.expiration as string))];
      const at = (d: string, sign: number) => {
        const t = Math.max(0, (Date.parse(d) - Date.parse(lastDate)) / 86_400_000);
        return { time: d as Time, value: spot + sign * (props.expectedMove as number) * Math.sqrt(t / span) };
      };
      for (const sign of [1, -1]) {
        const cone = c.addLineSeries({
          color: accent,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        cone.setData(pts.map((d) => at(d, sign)));
      }
    }

    const markers: SeriesMarker<Time>[] = [];
    if (data.earnings) {
      const d = data.earnings.date;
      const onChart = [lastDate, ...ahead].find((x) => x >= d);
      if (onChart && d > lastDate) {
        markers.push({
          time: onChart as Time,
          position: "belowBar",
          color: warn,
          shape: "arrowUp",
          text: `ER ${data.earnings.time === "bmo" ? "BMO" : data.earnings.time === "amc" ? "AMC" : ""}`.trim(),
        });
      }
    }
    future.setMarkers(markers);

    const end = ahead.length ? ahead[ahead.length - 1] : lastDate;
    c.timeScale().setVisibleRange({ from: addDays(lastDate, -range) as Time, to: end as Time });
    return () => {
      c.remove();
      chart.current = null;
      candles.current = null;
    };
    // The live price is applied below without rebuilding.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, props.expiration, props.expectedMove, range]);

  // Follow the live price on the last bar.
  useEffect(() => {
    const s = candles.current;
    if (!s || !data || !props.spot || !data.candles.length) return;
    const last = data.candles[data.candles.length - 1];
    const today = iso(new Date());
    const base = last.date < today ? { time: today as Time, open: last.close, high: last.close, low: last.close } : { time: last.date as Time, open: last.open, high: last.high, low: last.low };
    s.update({ ...base, high: Math.max(base.high, props.spot), low: Math.min(base.low, props.spot), close: props.spot });
  }, [props.spot, data]);

  return (
    <div className="price-chart">
      <div className="chart-bar">
        <div className="seg" role="group" aria-label="Chart range">
          {RANGES.map(([label, days]) => (
            <button key={label} aria-pressed={range === days} onClick={() => setRange(days)}>
              {label}
            </button>
          ))}
        </div>
        <span className="dim-text chart-legend">
          Daily · <span className="key-short">━</span> your shorts <span className="key-long">┅</span> longs ·{" "}
          <span className="key-cone">┅</span> expected move to {props.expiration ?? "–"} · ⋯ 52-week high/low ·{" "}
          <span className="key-short">▲</span> earnings
        </span>
      </div>
      {error ? <div className="msg warn">Chart: {error}</div> : null}
      <div className="chart-box" ref={box} />
    </div>
  );
}
