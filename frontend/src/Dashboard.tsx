import { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { getJson } from "./api";
import { AccountContext } from "./context";
import { pct, signed } from "./format";

interface Stats {
  n: number;
  win_rate: number | null;
  total: number;
  avg: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  per_day: number | null;
  kept: number | null;
}
interface Bucket extends Stats {
  bucket: string;
}
interface Trade {
  id: string;
  source: string;
  underlying: string;
  strategy: string;
  opened: string | null;
  closed: string | null;
  pl: number;
  dte: number | null;
  days: number;
  rolls: number;
  otm: number | null;
  vix: number | null;
  move: number | null;
  outcome: string;
}
interface Finding {
  factor: string;
  bucket: string;
  n: number;
  avg: number;
  avg_rest: number;
  win_rate: number;
  win_rate_rest: number;
  strength: "strong" | "moderate";
  better: boolean;
}
interface DashData {
  account: "live" | "sim";
  count: number;
  prices_pending: number | null;
  coverage: Record<string, number>;
  overview: {
    total: number;
    months: Record<string, Record<string, number>>;
    underlyings: [string, number][];
    cumulative: [string, number][];
    trades: Stats;
    strategies: Bucket[];
    losers: Trade[];
  };
  factors: { key: string; label: string; rows: Bucket[] }[];
  findings: Finding[];
  mechanics: { followed: Stats; not_followed: Stats };
}
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const GAIN = "#4493f8";
const LOSS = "#e5534b";

function compact(v: number): string {
  const a = Math.abs(v);
  const s = a >= 1e6 ? `${(a / 1e6).toFixed(1)}M` : a >= 1e4 ? `${Math.round(a / 1e3)}K` : a >= 1e3 ? `${(a / 1e3).toFixed(1)}K` : a.toFixed(0);
  return (v < 0 ? "−" : "") + s;
}

type Tab = "overview" | "worked";

export default function Dashboard() {
  const account = useContext(AccountContext);
  const [tab, setTab] = useState<Tab>("overview");
  const [futures, setFutures] = useState(false);
  const [data, setData] = useState<DashData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const q = account === "live" ? `account=live&futures=${futures}` : "account=sim";
      setData(await getJson<DashData>(`/api/dashboard?${q}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [account, futures]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="dashboard">
      <div className="toolbar">
        <div className="seg" role="tablist" aria-label="Dashboard view">
          <button aria-pressed={tab === "overview"} onClick={() => setTab("overview")}>
            Overview
          </button>
          <button aria-pressed={tab === "worked"} onClick={() => setTab("worked")}>
            What worked
          </button>

        </div>
        {account === "live" ? (
          <>
            <label className="check">
              <input type="checkbox" checked={futures} onChange={(e) => setFutures(e.target.checked)} /> Include futures options in totals
            </label>
          </>
        ) : (
          <span className="dim-text">Simulated account</span>
        )}
        {account === "live" ? <ExportForm /> : null}
      </div>
      {error ? <div className="banner warn">{error}</div> : null}
      {data && data.prices_pending ? (
        <div className="msg">Loading price history for the analysis: {data.prices_pending} symbols to go. VIX and underlying-move breakdowns fill in as it arrives.</div>
      ) : null}
      {!data ? (
        <div className="empty">{loading ? "Loading…" : "No data."}</div>
      ) : (
        <div className={loading ? "refetching" : ""}>
          {tab === "overview" ? <Overview d={data} /> : null}
          {tab === "worked" ? <Worked d={data} /> : null}
        </div>
      )}
    </div>
  );
}

function ExportForm() {
  const year = new Date().getFullYear();
  const [start, setStart] = useState(`${year}-01-01`);
  const [end, setEnd] = useState(new Date().toISOString().slice(0, 10));
  return (
    <form
      className="export"
      onSubmit={(e) => {
        e.preventDefault();
        window.location.href = `/api/export/sheet?start=${start}&end=${end}`;
      }}
    >
      <span className="dim-text">Export in the spreadsheet's format</span>
      <input type="date" value={start} onChange={(e) => setStart(e.target.value)} aria-label="From" />
      <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} aria-label="To" />
      <button type="submit" className="small">
        Download .xlsx
      </button>
    </form>
  );
}

// Overview -----------------------------------------------------------------------------

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      {sub ? <div className="tile-sub">{sub}</div> : null}
    </div>
  );
}

function Overview({ d }: { d: DashData }) {
  const o = d.overview;
  const t = o.trades;
  return (
    <>
      <div className="tiles">
        <Tile label="Realized P/L" value={signed(o.total)} sub="as each leg closed" />
        <Tile label="Closed trades" value={t.n.toLocaleString("en-US")} sub="counting rolls as one trade" />
        <Tile label="Win rate" value={pct(t.win_rate)} />
        <Tile label="Average winner" value={signed(t.avg_win)} sub={`average loser ${signed(t.avg_loss)}`} />
        <Tile label="P/L per day held" value={signed(t.per_day, 2)} />
      </div>
      <h3>Cumulative realized P/L</h3>
      <CumulativeChart points={o.cumulative} />
      <h3>By month</h3>
      <MonthTable months={o.months} />
      <div className="two-col">
        <div>
          <h3>By underlying</h3>
          <BarList rows={o.underlyings.slice(0, 20)} />
        </div>
        <div>
          <h3>By strategy</h3>
          <StatsTable rows={o.strategies} label="Strategy" />
        </div>
      </div>
      <h3>Largest losses</h3>
      <TradesTable trades={o.losers} />
    </>
  );
}

function CumulativeChart({ points }: { points: [string, number][] }) {
  const W = 960;
  const H = 240;
  const P = { l: 56, r: 16, t: 10, b: 24 };
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const geo = useMemo(() => {
    if (points.length < 2) return null;
    const xs = points.map((p) => new Date(p[0]).getTime());
    const ys = points.map((p) => p[1]);
    const x0 = xs[0];
    const x1 = xs[xs.length - 1];
    let lo = Math.min(0, ...ys);
    let hi = Math.max(0, ...ys);
    const pad = (hi - lo) * 0.06 || 1;
    lo -= pad;
    hi += pad;
    const sx = (v: number) => P.l + ((v - x0) / (x1 - x0 || 1)) * (W - P.l - P.r);
    const sy = (v: number) => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b);
    const step = niceStep((hi - lo) / 4);
    const ticks: number[] = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(v);
    const years: number[] = [];
    for (let y = new Date(x0).getFullYear() + 1; y <= new Date(x1).getFullYear(); y++) years.push(y);
    return { xs, ys, sx, sy, ticks, years };
  }, [points]);
  if (!geo) return <div className="empty">Not enough closed trades to chart.</div>;
  const { xs, ys, sx, sy, ticks, years } = geo;
  const line = xs.map((x, i) => `${i ? "L" : "M"}${sx(x).toFixed(1)},${sy(ys[i]).toFixed(1)}`).join("");
  const area = `${line}L${sx(xs[xs.length - 1]).toFixed(1)},${sy(0).toFixed(1)}L${sx(xs[0]).toFixed(1)},${sy(0).toFixed(1)}Z`;
  const onMove = (e: React.PointerEvent) => {
    const r = ref.current!.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    let best = 0;
    for (let i = 1; i < xs.length; i++) if (Math.abs(sx(xs[i]) - px) < Math.abs(sx(xs[best]) - px)) best = i;
    setHover(best);
  };
  const last = ys[ys.length - 1];
  return (
    <div className="chart cum">
      <svg ref={ref} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Cumulative realized P/L over time" onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={P.l} x2={W - P.r} y1={sy(v)} y2={sy(v)} className={v === 0 ? "zero" : "grid"} />
            <text x={P.l - 6} y={sy(v) + 4} className="axis" textAnchor="end">
              {compact(v)}
            </text>
          </g>
        ))}
        {years.map((y) => {
          const x = sx(new Date(`${y}-01-01`).getTime());
          return (
            <text key={y} x={x} y={H - 6} className="axis" textAnchor="middle">
              {y}
            </text>
          );
        })}
        <path d={area} fill={last >= 0 ? GAIN : LOSS} opacity={0.1} />
        <path d={line} fill="none" stroke={last >= 0 ? GAIN : LOSS} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        <circle cx={sx(xs[xs.length - 1])} cy={sy(last)} r={4} fill={last >= 0 ? GAIN : LOSS} stroke="var(--bg)" strokeWidth={2} />
        {hover !== null ? (
          <g>
            <line x1={sx(xs[hover])} x2={sx(xs[hover])} y1={P.t} y2={H - P.b} className="cross" />
            <circle cx={sx(xs[hover])} cy={sy(ys[hover])} r={4} fill={ys[hover] >= 0 ? GAIN : LOSS} stroke="var(--bg)" strokeWidth={2} />
          </g>
        ) : null}
      </svg>
      <div className="chart-legend">
        {hover !== null ? (
          <>
            <b>{signed(ys[hover])}</b>
            <span>{new Date(xs[hover]).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}</span>
          </>
        ) : (
          <span>
            Now <b>{signed(last)}</b>
          </span>
        )}
      </div>
    </div>
  );
}

function niceStep(raw: number): number {
  const mag = 10 ** Math.floor(Math.log10(Math.max(raw, 1e-9)));
  return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
}

function cellStyle(v: number | undefined, max: number): React.CSSProperties {
  if (!v) return {};
  const a = Math.min(1, Math.abs(v) / (max || 1));
  const rgb = v > 0 ? "68,147,248" : "229,83,75";
  return { background: `rgba(${rgb},${(0.12 + 0.5 * a).toFixed(3)})` };
}

function MonthTable({ months }: { months: Record<string, Record<string, number>> }) {
  const years = Object.keys(months).sort();
  const vals = years.flatMap((y) => Object.values(months[y]));
  const max = Math.max(1, ...vals.map(Math.abs));
  if (!years.length) return <div className="empty">No closed trades.</div>;
  return (
    <div className="table-wrap">
      <table className="positions heat">
        <thead>
          <tr>
            <th className="l">Month</th>
            {years.map((y) => (
              <th key={y}>{y}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {MONTHS.map((m, i) => (
            <tr key={m}>
              <td className="l">{m}</td>
              {years.map((y) => {
                const v = months[y][String(i + 1)];
                return (
                  <td key={y} style={cellStyle(v, max)} title={v === undefined ? "" : `${m} ${y}: ${signed(v)}`}>
                    {v === undefined ? "" : signed(v)}
                  </td>
                );
              })}
            </tr>
          ))}
          <tr className="total">
            <td className="l">Year</td>
            {years.map((y) => (
              <td key={y}>{signed(Object.values(months[y]).reduce((a, b) => a + b, 0))}</td>
            ))}
          </tr>
        </tbody>
      </table>
    </div>
  );
}

function BarList({ rows }: { rows: [string, number][] }) {
  const max = Math.max(1, ...rows.map((r) => Math.abs(r[1])));
  return (
    <div className="barlist">
      {rows.map(([label, v]) => (
        <div key={label} className="bar-row" tabIndex={0} title={`${label}: ${signed(v)}`}>
          <span className="bar-label">{label}</span>
          <span className="bar-track">
            <span className="bar-mid" />
            <span
              className="bar"
              style={{
                background: v >= 0 ? GAIN : LOSS,
                width: `${(Math.abs(v) / max) * 50}%`,
                left: v >= 0 ? "50%" : `${50 - (Math.abs(v) / max) * 50}%`,
                borderRadius: v >= 0 ? "0 4px 4px 0" : "4px 0 0 4px",
              }}
            />
          </span>
          <span className="bar-value">{signed(v)}</span>
        </div>
      ))}
    </div>
  );
}

function StatsTable({ rows, label }: { rows: Bucket[]; label: string }) {
  const max = Math.max(1, ...rows.map((r) => Math.abs(r.avg ?? 0)));
  return (
    <div className="table-wrap">
      <table className="positions statstable">
        <thead>
          <tr>
            <th className="l">{label}</th>
            <th>Trades</th>
            <th>Win rate</th>
            <th className="l">Average P/L</th>
            <th>Total</th>
            <th>Per day</th>
            <th title="Median share of the opening credit kept">Credit kept</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.bucket} className={r.n < 10 ? "thin" : ""} title={r.n < 10 ? "Fewer than 10 trades: too few to read much into" : ""}>
              <td className="l">{r.bucket}</td>
              <td>{r.n}</td>
              <td>{pct(r.win_rate)}</td>
              <td className="l avgbar">
                <span className="mini-track">
                  <span className="bar-mid" />
                  <span
                    className="bar"
                    style={{
                      background: (r.avg ?? 0) >= 0 ? GAIN : LOSS,
                      width: `${(Math.abs(r.avg ?? 0) / max) * 50}%`,
                      left: (r.avg ?? 0) >= 0 ? "50%" : `${50 - (Math.abs(r.avg ?? 0) / max) * 50}%`,
                    }}
                  />
                </span>
                <span className="v">{signed(r.avg)}</span>
              </td>
              <td>{signed(r.total)}</td>
              <td>{signed(r.per_day, 2)}</td>
              <td>{pct(r.kept)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TradesTable({ trades }: { trades: Trade[] }) {
  return (
    <div className="table-wrap">
      <table className="positions">
        <thead>
          <tr>
            <th className="l">Trade</th>
            <th>Opened</th>
            <th>Closed</th>
            <th>P/L</th>
            <th>DTE in</th>
            <th>Days</th>
            <th>Rolls</th>
            <th title="Short strike's distance out of the money at entry">OTM in</th>
            <th>VIX in</th>
            <th>Move</th>
            <th className="l">How it ended</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => (
            <tr key={t.id}>
              <td className="l">
                <span className="sym-inline">{t.underlying}</span>
                {t.strategy}
              </td>
              <td>{t.opened ?? "–"}</td>
              <td>{t.closed ?? "–"}</td>
              <td>{signed(t.pl)}</td>
              <td>{t.dte ?? "–"}</td>
              <td>{Math.round(t.days)}</td>
              <td>{t.rolls}</td>
              <td>{t.otm === null ? "–" : pct(t.otm, 1)}</td>
              <td>{t.vix === null ? "–" : t.vix.toFixed(1)}</td>
              <td>{t.move === null ? "–" : pct(t.move, 1)}</td>
              <td className="l">{t.outcome}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// What worked --------------------------------------------------------------------------

function Worked({ d }: { d: DashData }) {
  const [factor, setFactor] = useState("dte");
  const f = d.factors.find((x) => x.key === factor) ?? d.factors[0];
  const covered = d.coverage[factor];
  const m = d.mechanics;
  return (
    <>
      <h3>What stands out</h3>
      {d.findings.length ? (
        <ul className="findings">
          {d.findings.map((x, i) => (
            <li key={i} className={x.better ? "better" : "worse"}>
              <span className="mark" aria-hidden>
                {x.better ? "▲" : "▼"}
              </span>
              <span>
                <b>{x.factor}: {x.bucket}.</b> {x.n} trades averaged <b>{signed(x.avg)}</b> against {signed(x.avg_rest)} for the rest; win rate {pct(x.win_rate)} against{" "}
                {pct(x.win_rate_rest)}. <span className="sub">{x.strength === "strong" ? "Strong difference" : "Moderate difference"}</span>
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="dim-text">No factor stands out yet: either there are too few closed trades, or results do not differ by more than chance would explain.</p>
      )}
      <p className="dim-text">These show what went with better or worse results in past trades. They do not prove why; a pattern in a small group can be luck.</p>

      <h3>Followed the mechanics?</h3>
      <p className="dim-text">Opened at 30–60 DTE and closed before expiration, compared with every other credit trade.</p>
      <div className="tiles">
        <Tile label="Followed" value={signed(m.followed.avg)} sub={`average over ${m.followed.n} trades · win rate ${pct(m.followed.win_rate)}`} />
        <Tile label="Did not" value={signed(m.not_followed.avg)} sub={`average over ${m.not_followed.n} trades · win rate ${pct(m.not_followed.win_rate)}`} />
      </div>

      <h3>Break down by</h3>
      <div className="chips factor-chips" role="tablist" aria-label="Factor">
        {d.factors.map((x) => (
          <button key={x.key} role="tab" aria-selected={x.key === factor} onClick={() => setFactor(x.key)}>
            {x.label}
          </button>
        ))}
      </div>
      {covered !== undefined && covered < d.count ? (
        <p className="dim-text">
          Known for {covered.toLocaleString("en-US")} of {d.count.toLocaleString("en-US")} trades.
          {factor === "earnings_days" ? " Older trades have no earnings date; trades placed through the app record it." : ""}
          {factor === "ivr" || factor === "pop" ? " Older trades have it only for SPX and SPY, estimated from VIX; trades placed through the app record it." : ""}
          {factor === "vix" || factor === "move" ? " Fills in as price history loads." : ""}
        </p>
      ) : null}
      {f.rows.length ? <StatsTable rows={f.rows} label={f.label} /> : <div className="empty">No trades with this information yet.</div>}
    </>
  );
}


