import { useContext, useEffect, useState } from "react";
import { getJson, postJson } from "./api";
import { AccountContext, AccountsContext, TicketContext } from "./context";
import { money, pct, shortDate } from "./format";
import { proposalSpec } from "./OrderTicket";
import type { ProposalBody } from "./types";

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
  /** The trade alone ("Iron condor 660/665/765/770") and the reasons after it. */
  title?: string;
  detail?: string;
  body?: ProposalBody;
  /** The proposal waiting on Positions for this find, if one was made. */
  proposal_id?: string | null;
  view?: string | null;
  play?: string | null;
};

function dur(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

type ScanView = {
  level: "off" | "notify" | "propose" | "auto";
  settings: { scan_time: string; min_ivr: number; undefined_ivr: number };
  last: {
    at: number;
    level: string;
    candidates: Candidate[];
    skipped: { symbol: string; reason: string }[];
    acted: string[];
    took_s?: number;
    schwab_requests?: number;
    waited_s?: number;
  } | null;
  // A scan running in the background, and why the last one failed.
  running?: { done: number; total: number } | null;
  error?: string | null;
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
  const openTicket = useContext(TicketContext);

  useEffect(() => {
    setView(null);
    getJson<ScanView>(`/api/scanner?account=${account}`).then(setView, (e) =>
      setError(String(e.message)),
    );
  }, [account]);

  const dismiss = async (symbol: string) => {
    setError(null);
    try {
      setView(
        await postJson<ScanView>(
          `/api/scanner/dismiss?account=${account}&symbol=${encodeURIComponent(symbol)}`,
          {},
        ),
      );
    } catch (e) {
      setError(String((e as Error).message));
    }
  };

  // While a scan runs on the server, check on it every 2 seconds.
  const running = !!view?.running;
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => {
      getJson<ScanView>(`/api/scanner?account=${account}`).then(
        setView,
        () => {},
      );
    }, 2000);
    return () => window.clearInterval(id);
  }, [running, account]);

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
  const progress = view.running
    ? view.running.total
      ? `scanning ${view.running.done} of ${view.running.total}`
      : "scanning…"
    : null;
  const status = progress
    ? progress
    : last
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
        <button
          type="button"
          className="small"
          disabled={busy || running}
          onClick={scan}
        >
          {busy || running ? (
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
          {last && last.took_s != null ? (
            <>
              {" "}
              Last scan took {dur(last.took_s)}: {last.schwab_requests ?? 0} Schwab
              request{last.schwab_requests === 1 ? "" : "s"}
              {last.waited_s ? `, ${dur(last.waited_s)} of it waiting for Schwab's request limit` : ""}.
            </>
          ) : null}
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
      {view.error ? (
        <div className="msg warn">The scan stopped: {view.error}</div>
      ) : null}
      {last && last.candidates.length ? (
        <ul className="finds">
          {last.candidates.map((c) => {
            const status = c.note ?? done(last.acted, c.symbol);
            return (
              <li key={c.symbol} className="find">
                <div className="find-head">
                  <span className="find-title">
                    <span className="sym-inline">{c.symbol}</span>
                    {c.title ?? c.label}
                  </span>
                  {c.body ? (
                    <button
                      type="button"
                      className="small primary"
                      onClick={() =>
                        openTicket({
                          ...proposalSpec(
                            {
                              id: c.proposal_id,
                              underlying: c.symbol,
                              strategy: c.strategy,
                              view: c.view,
                              play: c.play,
                            },
                            c.body!,
                            account,
                          ),
                          // Reviewed: placed or closed, the find comes off the list.
                          onClosed: () => dismiss(c.symbol),
                        })
                      }
                    >
                      Review
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="rm"
                    title={
                      c.proposal_id
                        ? "Dismiss (and its proposal on Positions)"
                        : "Dismiss"
                    }
                    aria-label={`Dismiss ${c.symbol}`}
                    onClick={() => dismiss(c.symbol)}
                  >
                    ×
                  </button>
                </div>
                <div className="find-figs">
                  <span>
                    {shortDate(c.expiration)}{" "}
                    <span className="sub">{c.dte}d</span>
                  </span>
                  {c.mid === null ? null : (
                    <span>{Math.abs(c.mid).toFixed(2)} cr</span>
                  )}
                  {c.pop === null ? null : (
                    <span title="Probability of profit at entry">
                      POP {pct(c.pop)}
                    </span>
                  )}
                  {c.bp === null ? null : (
                    <span title="Buying power">BP {money(c.bp)}</span>
                  )}
                  <span>IVR {Math.round(c.ivr)}</span>
                  {status ? (
                    <span className="find-status">{status}</span>
                  ) : null}
                </div>
                {c.title && c.detail ? (
                  <div className="find-detail">{c.detail}</div>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
