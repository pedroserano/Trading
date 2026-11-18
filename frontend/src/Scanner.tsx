import { useContext, useEffect, useState } from "react";
import { getJson, postJson } from "./api";
import { AccountContext, AccountsContext } from "./context";
import { money, pct, shortDate } from "./format";

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
  score?: number;
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

const DONE: Record<string, string> = {
  notify: "Notified",
  propose: "Proposed",
  place: "Placed",
};

function done(acted: string[], symbol: string): string {
  const a = acted.find((x) => x.endsWith(` ${symbol}`));
  return a ? (DONE[a.split(" ")[0]] ?? "") : "";
}

/** The scanner's last look for new trades, and Scan now. */
export default function Scanner() {
  const account = useContext(AccountContext);
  const accountName =
    useContext(AccountsContext).find((a) => a.key === account)?.name ??
    "Schwab account";
  const [view, setView] = useState<ScanView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setView(null);
    getJson<ScanView>(`/api/scanner?account=${account}`).then(setView, (e) =>
      setError(String(e.message)),
    );
  }, [account]);

  const scan = async () => {
    setBusy(true);
    setError(null);
    try {
      setView(
        await postJson<ScanView>(`/api/scanner/run?account=${account}`, {}),
      );
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
  const short = {
    off: "Off",
    notify: "Notify",
    propose: "Propose",
    auto: "Auto",
  }[view.level];
  const status = last
    ? `${new Date(last.at * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })} · ${last.candidates.length} found`
    : "no scan yet";
  return (
    <div className="scanner">
      {/* One line; tap it for the schedule, thresholds and what was skipped. */}
      <div className="scan-bar">
        <button
          type="button"
          className="scan-sum"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          <b>Scanner</b>{" "}
          <span className={`scan-level ${view.level}`}>{short}</span>{" "}
          <span className="dim-text scan-status">
            {status}
            {last && last.skipped.length ? (
              <span className="ph-hide"> · {last.skipped.length} skipped</span>
            ) : null}
          </span>{" "}
          <span className="dim-text">{open ? "▴" : "▾"}</span>
        </button>
        <a
          href="#/settings/scanner"
          className="scan-gear"
          title="Scanner settings"
          aria-label="Scanner settings"
        >
          ⚙︎
        </a>
        <button type="button" className="small" disabled={busy} onClick={scan}>
          {busy ? (
            "Scanning…"
          ) : (
            <>
              Scan<span className="ph-hide"> now</span>
            </>
          )}
        </button>
      </div>
      {open ? (
        <div className="scan-more dim-text">
          {account !== "sim" ? accountName : "Simulated account"}: {what}. Daily
          at {view.settings.scan_time} ET; opens when IV rank ≥{" "}
          {view.settings.min_ivr}, strangles from {view.settings.undefined_ivr}.
          {last && last.skipped.length ? (
            <ul className="skipped">
              {last.skipped.map((x) => (
                <li key={x.symbol}>
                  <span className="sym-inline">{x.symbol}</span>
                  {x.reason}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      {error ? <div className="msg warn">{error}</div> : null}
      {last && last.candidates.length ? (
        <div className="table-wrap">
          <table className="positions fit ph-h2 ph-h3 ph-h6 ph-h7">
            <thead>
              <tr>
                <th className="l">Found</th>
                <th>IVR</th>
                <th>Exp</th>
                <th>Credit</th>
                <th title="Probability of profit at entry">POP</th>
                <th>Buying power</th>
                <th title="Ranking: IV rank, credit per dollar of buying power and probability of profit">
                  Score
                </th>
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
                    {shortDate(c.expiration)}{" "}
                    <span className="sub">{c.dte}d</span>
                  </td>
                  <td>{c.mid === null ? "" : Math.abs(c.mid).toFixed(2)}</td>
                  <td>{pct(c.pop)}</td>
                  <td>{money(c.bp)}</td>
                  <td>{c.score === undefined ? "–" : c.score.toFixed(2)}</td>
                  <td className="l dim-text">
                    {c.note ?? done(last.acted, c.symbol)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
