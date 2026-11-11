import { createContext, useContext, useEffect, useState } from "react";
import { getJson, postJson } from "./api";
import { clock } from "./format";
import type { AutoLevel, AutomationView, StrategyView } from "./types";

type Ctx = { view: AutomationView | null; update: (v: AutomationView) => void };

/** Automanage state, shared by the Positions toolbar and every position's actions row. */
export const AutomationContext = createContext<Ctx>({ view: null, update: () => {} });

export function useAutomation(account: "sim" | "live"): Ctx {
  const [view, setView] = useState<AutomationView | null>(null);
  useEffect(() => {
    setView(null);
    let stop = false;
    const load = async () => {
      try {
        const v = await getJson<AutomationView>(`/api/automation?account=${account}`);
        if (!stop) setView(v);
      } catch {
        /* shown as missing controls; the next poll retries */
      }
    };
    load();
    const t = window.setInterval(load, 10000);
    return () => {
      stop = true;
      window.clearInterval(t);
    };
  }, [account]);
  return { view, update: setView };
}

function legText(l: { symbol: string; quantity: number; effect: string }): string {
  const s = l.symbol.replace(/\s+/g, " ");
  return `${l.quantity > 0 ? "+" : ""}${l.quantity} ${s} ${l.effect === "open" ? "to open" : "to close"}`;
}

/** The Positions toolbar control and the proposals waiting for one tap. */
export function AutoBar() {
  const { view, update } = useContext(AutomationContext);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!view) return null;
  const live = view.account === "live";
  const run = async (url: string, body: unknown = {}) => {
    if (live && url.endsWith("/accept") && !window.confirm("Send this order to Schwab at mid?")) return;
    setBusy(true);
    setError(null);
    try {
      update(await postJson<AutomationView>(`${url}?account=${view.account}`, body));
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };
  const managed = Object.values(view.trades).filter((t) => t.enabled).length;
  return (
    <>
      <div className="toolbar auto-bar">
        <span className="dim-text">Automanage</span>
        <div className="seg" role="group" aria-label="Automation">
          <button aria-pressed={!view.paused} disabled={busy} onClick={() => run("/api/automation/pause", { paused: false })}>
            Running
          </button>
          <button aria-pressed={view.paused} disabled={busy} onClick={() => run("/api/automation/pause", { paused: true })}>
            Paused
          </button>
        </div>
        <span className="dim-text">
          {managed} trade{managed === 1 ? "" : "s"} · {view.orders_today}/{view.settings.daily_cap} orders today ·{" "}
          {view.in_window ? "in" : "outside"} its window {view.settings.window_start}–{view.settings.window_end} ET
          {view.early_close ? " (early close: stops at 12:30)" : ""}
          {live ? " · live: proposes only, every order waits for your tap" : ""}
        </span>
        <a href="#/settings/automation" className="toolbar-link">
          Automanage settings
        </a>
      </div>
      {error ? <div className="banner warn">{error}</div> : null}
      {view.blocked && !view.paused ? (
        <div className="banner warn">Automation is holding: {view.blocked}.</div>
      ) : null}
      {view.proposals.map((p) => (
        <div key={p.id} className="banner proposal">
          <div>
            <strong>
              {p.underlying} {p.strategy}
            </strong>{" "}
            {p.reason}.
            {p.due_at ? <span className="sub"> Places itself at {clock(p.due_at)} unless dismissed.</span> : null}
            {p.bodies.map((b, i) => (
              <div key={i} className="sub mono">
                {b.intent}: {b.legs.map(legText).join(", ")}
              </div>
            ))}
          </div>
          <div className="proposal-actions">
            <button type="button" className="small primary" disabled={busy} onClick={() => run(`/api/automation/proposal/${p.id}/accept`)}>
              {live ? "Send to Schwab at mid" : "Place at mid"}
            </button>
            <button type="button" className="small" disabled={busy} onClick={() => run(`/api/automation/proposal/${p.id}/dismiss`)}>
              Dismiss
            </button>
          </div>
        </div>
      ))}
    </>
  );
}

export function AutoBadge({ st }: { st: StrategyView }) {
  const { view } = useContext(AutomationContext);
  if (!view || st.trade_id === null || !view.trades[String(st.trade_id)]?.enabled) return null;
  return (
    <span className="auto-badge" title="Automanage is on for this trade">
      AUTO
    </span>
  );
}

const SHORT: Record<string, string> = {
  profit: "profit target",
  dte: "DTE",
  tested: "tested",
  loss: "loss",
  expiry: "expiry day",
};

/** One line: what each level covers, from the defaults in Settings. */
function summary(levels: Record<string, AutoLevel>): string {
  const parts = (["auto", "propose", "notify"] as AutoLevel[])
    .map((lv) => {
      const rules = Object.keys(SHORT).filter((r) => levels[r] === lv);
      return rules.length ? `${lv[0].toUpperCase() + lv.slice(1)}: ${rules.map((r) => SHORT[r]).join(", ")}` : null;
    })
    .filter(Boolean);
  return parts.length ? parts.join(" · ") : "Every rule is off in Settings";
}

/** The Automanage switch for one position; the rules themselves are set in Settings. */
export function AutoPanel({ st }: { st: StrategyView }) {
  const { view, update } = useContext(AutomationContext);
  const [error, setError] = useState<string | null>(null);
  if (!view || st.trade_id === null) return null;
  const enabled = !!view.trades[String(st.trade_id)]?.enabled;
  const save = async (on: boolean) => {
    setError(null);
    try {
      update(
        await postJson<AutomationView>(`/api/automation/trade?account=${view.account}`, {
          trade_id: st.trade_id,
          enabled: on,
        }),
      );
    } catch (e) {
      setError(String((e as Error).message));
    }
  };
  return (
    <span className="auto-panel" onClick={(e) => e.stopPropagation()}>
      <label className="check">
        <input type="checkbox" checked={enabled} onChange={(e) => save(e.target.checked)} />
        Automanage
      </label>
      {enabled ? (
        <a className="sub" href="#/settings/automation" title="Change these in Settings">
          {summary(view.settings.levels)}
        </a>
      ) : null}
      {error ? <span className="warn-text"> {error}</span> : null}
    </span>
  );
}

/** The header's kill switch: pauses or resumes automation for the account on screen. */
export function PauseSwitch() {
  const { view, update } = useContext(AutomationContext);
  const [busy, setBusy] = useState(false);
  if (!view) return null;
  const held = view.paused || !!view.blocked;
  const toggle = async () => {
    setBusy(true);
    try {
      update(
        await postJson<AutomationView>(`/api/automation/pause?account=${view.account}`, {
          paused: !view.paused,
        }),
      );
    } catch {
      /* the next poll shows the real state */
    } finally {
      setBusy(false);
    }
  };
  const title = view.paused
    ? "Automation is paused for this account. Click to resume."
    : view.blocked
      ? `Automation is holding: ${view.blocked}. Click to pause it too.`
      : "Automation is running for this account. Click to pause it.";
  return (
    <button type="button" className={`pause-switch ${held ? "held" : ""}`} disabled={busy} onClick={toggle} title={title}>
      {view.paused ? "Auto paused" : view.blocked ? "Auto holding" : "Auto on"}
    </button>
  );
}
