import { useContext, useEffect, useState } from "react";
import { getJson, postJson } from "./api";
import { AccountContext } from "./context";
import { clock, money, pct } from "./format";

type Candidate = {
  symbol: string;
  ivr: number;
  expiration: string;
  dte: number;
  label: string;
  strategy: string;
  mid: number | null;
  bp: number | null;
  pop: number | null;
  note?: string;
};

type ScanView = {
  level: "off" | "notify" | "propose" | "auto";
  settings: { scan_time: string; min_ivr: number; undefined_ivr: number };
  last: {
    at: number;
    level: string;
    candidates: Candidate[];
    skipped: { symbol: string; reason: string }[];
    acted: string[];
  } | null;
};

const DONE: Record<string, string> = { notify: "Notified", propose: "Proposed", place: "Placed" };

function done(acted: string[], symbol: string): string {
  const a = acted.find((x) => x.endsWith(` ${symbol}`));
  return a ? DONE[a.split(" ")[0]] ?? "" : "";
}

/** The scanner's last look for new trades, and Scan now. */
export default function Scanner() {
  const account = useContext(AccountContext);
  const [view, setView] = useState<ScanView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showSkipped, setShowSkipped] = useState(false);

  useEffect(() => {
    setView(null);
    getJson<ScanView>(`/api/scanner?account=${account}`).then(setView, (e) => setError(String(e.message)));
  }, [account]);

  const scan = async () => {
    setBusy(true);
    setError(null);
    try {
      setView(await postJson<ScanView>(`/api/scanner/run?account=${account}`, {}));
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  if (!view) return error ? <div className="banner warn">{error}</div> : null;
  const last = view.last;
  const what =
    view.level === "off"
      ? "off: it only shows what it finds"
      : view.level === "notify"
        ? "notifies you of what it finds"
        : view.level === "propose"
          ? "proposes what it finds on Positions, for one tap"
          : "places what it finds";
  return (
    <div className="scanner">
      <div className="toolbar">
        <span>
          <b>Scanner</b>{" "}
          <span className="dim-text">
            {account === "live" ? "Schwab account" : "Simulated account"}: {what}. Daily at {view.settings.scan_time} ET;
            opens when IV rank ≥ {view.settings.min_ivr}, strangles from {view.settings.undefined_ivr}.
            {last ? ` Last scan ${clock(last.at)}.` : " No scan yet."}
          </span>
        </span>
        <span>
          <a href="#/settings/scanner" className="toolbar-link">
            Scanner settings
          </a>{" "}
          <button type="button" className="small" disabled={busy} onClick={scan}>
            {busy ? "Scanning…" : "Scan now"}
          </button>
        </span>
      </div>
      {error ? <div className="msg warn">{error}</div> : null}
      {last && last.candidates.length ? (
        <div className="table-wrap">
          <table className="positions">
            <thead>
              <tr>
                <th className="l">Found</th>
                <th>IVR</th>
                <th>Exp</th>
                <th>Credit</th>
                <th title="Probability of profit at entry">POP</th>
                <th>Buying power</th>
                <th className="l" />
              </tr>
            </thead>
            <tbody>
              {last.candidates.map((c) => (
                <tr key={c.symbol}>
                  <td className="l">
                    <span className="sym-inline">{c.symbol}</span>
                    {c.label}
                  </td>
                  <td>{Math.round(c.ivr)}</td>
                  <td>
                    {c.expiration.slice(5)} <span className="sub">{c.dte}d</span>
                  </td>
                  <td>{c.mid === null ? "–" : Math.abs(c.mid).toFixed(2)}</td>
                  <td>{pct(c.pop)}</td>
                  <td>{money(c.bp)}</td>
                  <td className="l dim-text">
                    {c.note ?? done(last.acted, c.symbol)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : last ? (
        <p className="dim-text">Nothing qualified in the last scan.</p>
      ) : null}
      {last && last.skipped.length ? (
        <p className="dim-text">
          <button type="button" className="link" onClick={() => setShowSkipped(!showSkipped)}>
            {showSkipped ? "Hide" : "Show"} {last.skipped.length} skipped
          </button>
        </p>
      ) : null}
      {showSkipped && last ? (
        <ul className="skipped">
          {last.skipped.map((s) => (
            <li key={s.symbol}>
              <span className="sym-inline">{s.symbol}</span>
              {s.reason}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
