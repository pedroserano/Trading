import { useCallback, useContext, useEffect, useState } from "react";
import { getJson } from "./api";
import { AccountContext } from "./context";
import { money, pct, shortDate, signed, tone } from "./format";
import type { Band, Bands } from "./types";

interface Row {
  symbol: string;
  sector: string;
  bp: number | null;
  bp_pct: number | null;
  delta_bw: number | null;
  delta_pct: number | null;
  theta: number | null;
  theta_pct: number | null;
  trades: number;
}
interface SectorRow {
  sector: string;
  bp: number;
  bp_pct: number | null;
  delta_bw: number;
  delta_pct: number | null;
  theta: number;
  symbols: string[];
}
interface Position {
  underlying: string;
  name: string;
  trade_id: string | number | null;
  expiration: string | null;
  dte: number | null;
  delta_bw: number | null;
  delta_pct: number | null;
  theta: number | null;
  bp: number | null;
  flags: string[];
}
interface Rung {
  expiration: string;
  dte: number | null;
  theta: number;
  theta_pct: number | null;
  trades: number;
  bp: number;
}
interface Week {
  week: string;
  label: string;
  trades: { underlying: string; name: string; expiration: string; dte: number | null; crosses: string }[];
}
interface PortfolioData {
  net_liq: number | null;
  bp_used_pct: number | null;
  bp_ceiling_pct: number | null;
  bands: Bands | null;
  manage_dte: number;
  underlyings: Row[];
  sectors: SectorRow[];
  look_through?: { symbol: string; direct_bp: number; via_bp: number; total_pct: number | null; via: string[] }[];
  look_through_as_of?: string;
  positions: Position[];
  ladder: Rung[];
  calendar: Week[];
}

function Bar({ value, max, warn }: { value: number | null; max: number; warn?: boolean }) {
  const w = value === null || max <= 0 ? 0 : Math.min(100, (Math.abs(value) / max) * 100);
  return (
    <span className="meter wide">
      <span className={warn ? "over" : ""} style={{ width: `${w}%` }} />
    </span>
  );
}

function BandLine({ label, b, digits }: { label: string; b: Band | undefined; digits: number }) {
  if (!b) return null;
  const out = b.state === "below" || b.state === "above";
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">
        <span className={out ? "warn-text" : ""}>{pct(b.value, digits)}</span>
        <span className="sub">
          {" "}
          target {pct(b.min, digits)} to {pct(b.max, digits)}
        </span>
      </div>
    </div>
  );
}

/** Portfolio screen: where buying power, delta and theta sit, and what reaches 21 DTE when. */
export default function Portfolio() {
  const account = useContext(AccountContext);
  const [data, setData] = useState<PortfolioData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await getJson<PortfolioData>(`/api/portfolio?account=${account}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [account]);

  useEffect(() => {
    setData(null);
    load();
    const t = window.setInterval(load, 10000);
    return () => window.clearInterval(t);
  }, [load]);

  if (error) return <div className="msg bad">Portfolio could not be loaded: {error}</div>;
  if (!data) return <div className="empty">Loading…</div>;
  if (!data.underlyings.length) return <div className="empty">No open positions.</div>;

  const maxBp = Math.max(0.0001, ...data.underlyings.map((r) => r.bp_pct ?? 0));
  const maxSec = Math.max(0.0001, ...data.sectors.map((s) => s.bp_pct ?? 0));
  const maxDelta = Math.max(1, ...data.positions.map((p) => Math.abs(p.delta_bw ?? 0)));
  const maxTheta = Math.max(1, ...data.ladder.map((r) => Math.abs(r.theta)));

  return (
    <div className="portfolio">
      <div className="stats portfolio-stats">
        <div className="stat">
          <div className="stat-label">BP used</div>
          <div className="stat-value">
            <span className={(data.bp_used_pct ?? 0) > (data.bp_ceiling_pct ?? 1) ? "warn-text" : ""}>
              {pct(data.bp_used_pct)}
            </span>
            <span className="sub"> ceiling {pct(data.bp_ceiling_pct)}</span>
          </div>
        </div>
        <BandLine label="β-Δ of net liq" b={data.bands?.delta} digits={1} />
        <BandLine label="Θ of net liq a day" b={data.bands?.theta} digits={2} />
      </div>

      <div className="two-col">
        <section>
          <h3>Buying power by underlying</h3>
          <div className="table-wrap">
            <table className="positions compact">
              <thead>
                <tr>
                  <th className="l">Underlying</th>
                  <th className="l">Sector</th>
                  <th>BP</th>
                  <th>% net liq</th>
                  <th>β-Δ</th>
                  <th>Θ</th>
                </tr>
              </thead>
              <tbody>
                {data.underlyings.map((r) => (
                  <tr key={r.symbol}>
                    <td className="l">
                      <a className="sym" href={`#/trade/${encodeURIComponent(r.symbol)}`}>
                        {r.symbol}
                      </a>
                    </td>
                    <td className="l dim-text">{r.sector}</td>
                    <td>{money(r.bp)}</td>
                    <td>
                      {pct(r.bp_pct, 1)}
                      <Bar value={r.bp_pct} max={maxBp} />
                    </td>
                    <td className={tone(r.delta_bw)}>{signed(r.delta_bw, 0)}</td>
                    <td className={tone(r.theta)}>{signed(r.theta, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="footnote">
            Buying power is each trade's max loss when defined, otherwise the Reg T estimate;
            Schwab's maintenance requirement in the header is the figure that counts.
          </p>
        </section>
        <section>
          <h3>By sector</h3>
          <div className="table-wrap">
            <table className="positions compact">
              <thead>
                <tr>
                  <th className="l">Sector</th>
                  <th>% net liq</th>
                  <th>β-Δ</th>
                  <th>Θ</th>
                  <th className="l">Underlyings</th>
                </tr>
              </thead>
              <tbody>
                {data.sectors.map((s) => (
                  <tr key={s.sector}>
                    <td className="l">{s.sector}</td>
                    <td>
                      {pct(s.bp_pct, 1)}
                      <Bar value={s.bp_pct} max={maxSec} />
                    </td>
                    <td className={tone(s.delta_bw)}>{signed(s.delta_bw, 0)}</td>
                    <td className={tone(s.theta)}>{signed(s.theta, 0)}</td>
                    <td className="l dim-text">{s.symbols.join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.look_through && data.look_through.length ? (
            <>
              <h3
                className="sub-h"
                title={`Index funds hold their largest stocks: a SPY position is partly an NVDA position. Weights are approximate, as of ${data.look_through_as_of}. These count toward each stock's buying-power and notional limits.`}
              >
                Look-through: stocks inside your SPY and QQQ
              </h3>
              <div className="table-wrap">
                <table className="positions compact">
                  <thead>
                    <tr>
                      <th className="l">Stock</th>
                      <th>Held directly</th>
                      <th>Through index funds</th>
                      <th>% net liq</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.look_through.map((r) => (
                      <tr key={r.symbol}>
                        <td className="l">{r.symbol}</td>
                        <td>{r.direct_bp ? money(r.direct_bp) : "–"}</td>
                        <td title={r.via.join(", ")}>{money(r.via_bp)}</td>
                        <td>{pct(r.total_pct, 1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : null}
        </section>
      </div>

      <div className="two-col">
        <section>
          <h3>Beta-weighted delta by position</h3>
          <div className="table-wrap">
            <table className="positions compact">
              <thead>
                <tr>
                  <th className="l">Position</th>
                  <th>DTE</th>
                  <th>β-Δ SPY</th>
                  <th>% net liq</th>
                  <th>Θ</th>
                </tr>
              </thead>
              <tbody>
                {data.positions.map((p, i) => (
                  <tr key={`${p.trade_id ?? p.underlying}-${i}`}>
                    <td className="l">
                      {p.underlying} {p.name}
                      {p.flags.length ? <span className="dim-text"> · {p.flags.join(", ")}</span> : null}
                    </td>
                    <td>{p.dte ?? "–"}</td>
                    <td className={tone(p.delta_bw)}>
                      {signed(p.delta_bw, 1)}
                      <Bar value={p.delta_bw} max={maxDelta} />
                    </td>
                    <td className={tone(p.delta_pct)}>{pct(p.delta_pct, 1)}</td>
                    <td className={tone(p.theta)}>{signed(p.theta, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
        <section>
          <h3>Theta ladder</h3>
          <div className="table-wrap">
            <table className="positions compact">
              <thead>
                <tr>
                  <th className="l">Expiration</th>
                  <th>DTE</th>
                  <th>Trades</th>
                  <th>Θ / day</th>
                  <th>% net liq</th>
                  <th>BP</th>
                </tr>
              </thead>
              <tbody>
                {data.ladder.map((r) => (
                  <tr key={r.expiration}>
                    <td className="l">{shortDate(r.expiration)}</td>
                    <td className={r.dte !== null && r.dte <= data.manage_dte ? "warn-text" : ""}>{r.dte ?? "–"}</td>
                    <td>{r.trades}</td>
                    <td className={tone(r.theta)}>
                      {signed(r.theta, 1)}
                      <Bar value={r.theta} max={maxTheta} />
                    </td>
                    <td>{pct(r.theta_pct, 3)}</td>
                    <td>{money(r.bp)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <section>
        <h3>Reaching {data.manage_dte} DTE</h3>
        <div className="calendar">
          {data.calendar
            .filter((w) => w.week !== "later" || w.trades.length)
            .map((w) => (
              <div key={w.week} className={`week ${w.week === "now" && w.trades.length ? "due" : ""}`}>
                <div className="week-label">
                  {w.week === "now" || w.week === "later" ? w.label : `Week of ${shortDate(w.week)}`}
                </div>
                {w.trades.length ? (
                  w.trades.map((t, i) => (
                    <div key={i} className="week-trade" title={`Reaches ${data.manage_dte} DTE on ${t.crosses}`}>
                      {t.underlying} {t.name} <span className="dim-text">{shortDate(t.expiration)}</span>
                    </div>
                  ))
                ) : (
                  <div className="dim-text">–</div>
                )}
              </div>
            ))}
        </div>
      </section>
    </div>
  );
}
