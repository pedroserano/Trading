import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { getJson, postJson } from "./api";
import { TicketContext } from "./context";
import { clock } from "./format";
import { proposalSpec } from "./OrderTicket";
import { usePolling } from "./usePolling";
import type { AutoLevel, AutomationView, StrategyView } from "./types";

export type AutomationCtx = { view: AutomationView | null; update: (v: AutomationView) => void };

/** Automanage state, shared by the Positions toolbar and every position's actions row. */
export const AutomationContext = createContext<AutomationCtx>({ view: null, update: () => {} });

/** Under All: each Schwab account's automation, by account key. */
export const AutomationsContext = createContext<Record<string, AutomationCtx>>({});

/** A trade's own account's automation: under All, the account it is in. */
function useAutomationFor(st: StrategyView): AutomationCtx {
  const one = useContext(AutomationContext);
  const all = useContext(AutomationsContext);
  return (st.account && all[st.account]) || one;
}

/** Every listed account's automation, read every 10 seconds (All in the picker). */
export function useAllAutomations(accounts: string[]): Record<string, AutomationCtx> {
  const [views, setViews] = useState<Record<string, AutomationView>>({});
  const key = accounts.join(",");
  const load = useCallback(
    async (current: () => boolean) => {
      if (!key) return;
      const out: Record<string, AutomationView> = {};
      await Promise.all(
        key.split(",").map(async (a) => {
          try {
            out[a] = await getJson<AutomationView>(`/api/automation?account=${a}&log=0`);
          } catch {
            /* the next poll retries */
          }
        }),
      );
      if (current()) setViews(out);
    },
    [key],
  );
  usePolling(load, 10000);
  const ctxs: Record<string, AutomationCtx> = {};
  for (const a of key ? key.split(",") : []) {
    ctxs[a] = { view: views[a] ?? null, update: (v) => setViews((m) => ({ ...m, [a]: v })) };
  }
  return ctxs;
}

export function useAutomation(account: string): AutomationCtx {
  const [view, setView] = useState<AutomationView | null>(null);
  useEffect(() => setView(null), [account]);
  // The app never shows the automation log, so the poll leaves it out (log=0).
  const load = useCallback(
    async (current: () => boolean) => {
      try {
        const v = await getJson<AutomationView>(`/api/automation?account=${account}&log=0`);
        if (current()) setView(v);
      } catch {
        /* shown as missing controls; the next poll retries */
      }
    },
    [account],
  );
  usePolling(load, 10000);
  return { view, update: setView };
}

function legText(l: { symbol: string; quantity: number; effect: string }): string {
  const s = l.symbol.replace(/\s+/g, " ");
  return `${l.quantity > 0 ? "+" : ""}${l.quantity} ${s} ${l.effect === "open" ? "to open" : "to close"}`;
}

/** The Positions toolbar control and the proposals waiting for one tap. */
export function AutoBar() {
  const { view, update } = useContext(AutomationContext);
  const openTicket = useContext(TicketContext);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!view) return null;
  const live = view.account !== "sim";
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
  // Nothing managed and nothing sent today: the line would only describe an idle state.
  const idle = managed === 0 && view.orders_today === 0;
  return (
    <>
      {idle ? null : (
      <div
        className="toolbar auto-bar"
        title={
          `Trades ${view.settings.window_start}–${view.settings.window_end} ET` +
          (view.early_close ? " (early close: stops at 12:30)" : "") +
          (live
            ? view.max_level === "auto"
              ? ". Schwab: automatic orders allowed (LIVE_AUTO)."
              : ". Schwab: no automatic orders (LIVE_AUTO off), Auto not offered."
            : ".")
        }
      >
        <span className="dim-text">
          Automanage · {managed} trade{managed === 1 ? "" : "s"} · {view.orders_today}/{view.settings.daily_cap} orders
          {view.in_window ? "" : " · off hours"}
        </span>
        <a href="#/settings/automation" className="toolbar-link" aria-label="Automanage settings" title="Automanage settings">
          ⚙
        </a>
      </div>
      )}
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
            {p.bodies.length === 1 ? (
              // One order: open it on the ticket, to see the legs, price, risk and checks first.
              <button type="button" className="small primary" disabled={busy} onClick={() =>
                  openTicket({
                    ...proposalSpec(p, p.bodies[0], view.account),
                    // Placed: the proposal is settled, so read the list again.
                    onClosed: (placed) => {
                      if (placed)
                        getJson<AutomationView>(`/api/automation?account=${view.account}&log=0`).then(update, () => {});
                    },
                  })
                }>
                Review order
              </button>
            ) : (
              <button type="button" className="small primary" disabled={busy} onClick={() => run(`/api/automation/proposal/${p.id}/accept`)}>
                {live ? "Send to Schwab at mid" : "Place at mid"}
              </button>
            )}
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
  const { view } = useAutomationFor(st);
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
  earnings: "earnings",
  dividend: "ex-dividend",
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
  const { view, update } = useAutomationFor(st);
  const [error, setError] = useState<string | null>(null);
  if (!view || st.trade_id === null) return null;
  const trade = view.trades[String(st.trade_id)];
  const enabled = !!trade?.enabled;
  const post = async (body: { enabled?: boolean; view?: string }) => {
    setError(null);
    try {
      update(
        await postJson<AutomationView>(`/api/automation/trade?account=${view.account}`, {
          trade_id: st.trade_id,
          ...body,
        }),
      );
    } catch (e) {
      setError(String((e as Error).message));
    }
  };
  const save = (on: boolean) => post({ enabled: on });
  return (
    <span className="auto-panel" onClick={(e) => e.stopPropagation()}>
      <label className="check" title={enabled ? summary(view.settings.levels) : "Let automanage run this trade's rules"}>
        <input type="checkbox" checked={enabled} onChange={(e) => save(e.target.checked)} />
        Automanage
      </label>
      {enabled ? (
        <select
          className="view-select"
          value={trade?.view ?? "portfolio"}
          onChange={(e) => post({ view: e.target.value })}
          title="This trade's view: a defensive roll leaves it leaning this way. Portfolio follows the delta bias in Settings."
          aria-label="This trade's view"
        >
          <option value="portfolio">View: portfolio</option>
          <option value="neutral">View: neutral</option>
          <option value="bullish">View: bullish</option>
          <option value="bearish">View: bearish</option>
          <option value="would_own">View: would own (cash-secured)</option>
        </select>
      ) : null}
      {enabled ? (
        <a
          className="sub"
          href="#/settings/automation"
          title={`${summary(view.settings.levels)}. Change these in Settings.`}
        >
          Rules
        </a>
      ) : null}
      {error ? <span className="warn-text"> {error}</span> : null}
    </span>
  );
}

/** The header's kill switch: pauses or resumes automation for the account on screen. */
/** The header's automation switch for the account: Off, Propose or Auto. */
export function PauseSwitch() {
  const { view, update } = useContext(AutomationContext);
  const [busy, setBusy] = useState(false);
  if (!view) return null;
  const mode = view.paused ? "off" : (view.mode ?? "auto");
  const set = async (next: string) => {
    setBusy(true);
    try {
      update(await postJson<AutomationView>(`/api/automation/mode?account=${view.account}`, { mode: next }));
    } catch {
      /* the next poll shows the real state */
    } finally {
      setBusy(false);
    }
  };
  const title =
    mode === "off"
      ? view.paused && view.mode !== "off"
        ? "Automation paused itself (stale prices or the daily order cap). Pick a level to resume."
        : "Off: nothing automatic on this account."
      : (mode === "propose"
          ? "Propose: every rule and the scanner wait for your tap."
          : "Auto: each rule and the scanner act at their own level (Settings, Advanced).") +
        (view.auto_possible === false ? " On needs LIVE_TRADING=true and LIVE_AUTO=true in .env." : "") +
        (view.blocked ? ` Holding now: ${view.blocked}.` : "");
  return (
    <select
      className={`pause-switch mode-${mode} ${view.blocked && mode !== "off" ? "held" : ""}`}
      value={mode}
      disabled={busy}
      onChange={(e) => set(e.target.value)}
      title={title}
      aria-label="Automation for this account"
    >
      <option value="off">Auto: Off</option>
      <option value="propose">Auto: Propose</option>
      <option value="auto" disabled={view.auto_possible === false}>
        Auto: On
      </option>
    </select>
  );
}
