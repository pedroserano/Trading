import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getJson, postJson } from "./api";
import { money, num, pct, shortDate, strike } from "./format";
import type { Chain, Expiration, OrderLeg, Preview, SimOrder, StrategyView } from "./types";

export interface TicketSpec {
  title: string;
  account: "sim" | "live";
  intent: "open" | "close" | "roll" | "adjust";
  underlying: string;
  tradeId: number | string | null;
  legs: OrderLeg[];
  /** For rolls: the strategy being rolled, so the new expiration can be chosen. */
  roll?: StrategyView;
}

function netLabel(v: number | null): string {
  if (v === null) return "–";
  return `${Math.abs(v).toFixed(2)} ${v > 0 ? "cr" : v < 0 ? "db" : ""}`.trim();
}

/** Schwab and thinkorswim accept at most four legs in one order. */
export const MAX_LEGS = 4;

export type Side = "P" | "C" | null;

function sideLegs(st: StrategyView, side: Side) {
  return st.legs.filter((l) => l.kind === "OPTION" && (side === null || l.put_call === side));
}

function sideName(side: Side): string {
  return side === "P" ? " puts" : side === "C" ? " calls" : "";
}

/** Close the option legs of a strategy, or of one side of it. */
export function closeSpec(st: StrategyView, underlying: string, account: "sim" | "live", side: Side = null): TicketSpec {
  return {
    title: `Close ${underlying} ${st.name}${sideName(side)}`,
    account,
    intent: "close",
    underlying,
    tradeId: st.trade_id,
    legs: sideLegs(st, side).map((l) => ({ symbol: l.symbol, quantity: -l.quantity, effect: "close" as const })),
  };
}

/** Roll the option legs of a strategy, or of one side of it (closing and opening legs count toward the limit). */
export function rollSpec(st: StrategyView, underlying: string, account: "sim" | "live", side: Side = null): TicketSpec {
  return {
    ...closeSpec(st, underlying, account, side),
    title: `Roll ${underlying} ${st.name}${sideName(side)}`,
    intent: "roll",
    roll: { ...st, legs: sideLegs(st, side) },
  };
}

/** Which close and roll actions fit within the four-leg limit for a strategy. */
export function actionsFor(st: StrategyView): { close: Side[]; roll: Side[] } {
  const all = sideLegs(st, null).length;
  const puts = sideLegs(st, "P").length;
  const calls = sideLegs(st, "C").length;
  const close: Side[] = all <= MAX_LEGS ? [null] : ([puts && puts <= MAX_LEGS ? "P" : null, calls && calls <= MAX_LEGS ? "C" : null].filter(Boolean) as Side[]);
  // A roll closes and reopens every leg, so it doubles the leg count.
  const roll: Side[] =
    all * 2 <= MAX_LEGS ? [null] : ([puts && puts * 2 <= MAX_LEGS ? "P" : null, calls && calls * 2 <= MAX_LEGS ? "C" : null].filter(Boolean) as Side[]);
  return { close, roll };
}

function occ(underlying: string, exp: string, pc: string, k: number): string {
  const [y, m, d] = exp.split("-");
  return `${underlying.padEnd(6, " ")}${y.slice(2)}${m}${d}${pc}${String(Math.round(k * 1000)).padStart(8, "0")}`;
}

export default function OrderTicket({ spec, onClose }: { spec: TicketSpec; onClose: (placed?: SimOrder) => void }) {
  const [legs, setLegs] = useState<OrderLeg[]>(spec.legs);
  const [limit, setLimit] = useState<number | null>(null);
  const [duration, setDuration] = useState<"DAY" | "GTC">("DAY");
  // "Automanage on fill": the default comes from Settings, Automation. The Profit target rule
  // then closes the trade at its target (50%, 25% for straddles), at the level set there.
  const [manage, setManage] = useState(false);
  const [profitLevel, setProfitLevel] = useState<string | null>(null);
  useEffect(() => {
    if (spec.intent !== "open") return;
    getJson<{ settings: { ticket_automanage?: boolean; levels: Record<string, string> } }>(
      `/api/automation?account=${spec.account}`,
    )
      .then((v) => {
        setProfitLevel(v.settings.levels.profit ?? null);
        setManage(!!v.settings.ticket_automanage);
      })
      .catch(() => undefined);
  }, [spec.intent, spec.account]);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [placed, setPlaced] = useState<SimOrder | null>(null);
  const panel = useRef<HTMLDivElement>(null);

  // Roll: choose the new expiration and strikes.
  const [exps, setExps] = useState<Expiration[]>([]);
  const [rollExp, setRollExp] = useState<string | null>(null);
  const [chain, setChain] = useState<Chain | null>(null);
  const [shift, setShift] = useState<Record<string, number>>({});
  const current = spec.roll?.numbers.expiration ?? null;

  useEffect(() => {
    if (!spec.roll) return;
    getJson<{ expirations: Expiration[] }>(`/api/trade/${encodeURIComponent(spec.underlying)}/expirations`)
      .then((r) => {
        const later = r.expirations.filter((e) => !current || e.date > current);
        setExps(later);
        const monthlies = later.filter((e) => e.monthly);
        const pool = monthlies.length ? monthlies : later;
        if (pool.length) setRollExp(pool.reduce((a, b) => (Math.abs(b.dte - 45) < Math.abs(a.dte - 45) ? b : a)).date);
      })
      .catch((e) => setError((e as Error).message));
  }, [spec.roll, spec.underlying, current]);

  useEffect(() => {
    if (!spec.roll || !rollExp) return;
    getJson<Chain>(`/api/trade/${encodeURIComponent(spec.underlying)}/chain?expiration=${rollExp}`)
      .then(setChain)
      .catch((e) => setError((e as Error).message));
  }, [spec.roll, rollExp, spec.underlying]);

  const strikes = useMemo(() => (chain ? chain.rows.map((r) => r.strike) : []), [chain]);

  useEffect(() => {
    if (!spec.roll || !rollExp || !strikes.length) return;
    const opening: OrderLeg[] = spec.roll.legs
      .filter((l) => l.kind === "OPTION" && l.strike !== null)
      .map((l) => {
        // Same strike if listed, else the nearest; then any shift chosen on the ticket.
        let i = strikes.reduce((bi, k, j) => (Math.abs(k - l.strike!) < Math.abs(strikes[bi] - l.strike!) ? j : bi), 0);
        i = Math.max(0, Math.min(strikes.length - 1, i + (shift[l.symbol] ?? 0)));
        return { symbol: occ(spec.underlying, rollExp, l.put_call!, strikes[i]), quantity: l.quantity, effect: "open" as const };
      });
    setLegs([...spec.legs, ...opening]);
    setLimit(null);
  }, [spec.roll, spec.legs, spec.underlying, rollExp, strikes, shift]);

  const run = useCallback(
    async (lim: number | null) => {
      if (spec.roll && legs.length === spec.legs.length) return; // roll legs not built yet
      setBusy(true);
      try {
        const p = await postJson<Preview>("/api/orders/preview", {
          account: spec.account,
          intent: spec.intent,
          trade_id: spec.tradeId,
          legs,
          limit_net: lim,
          duration,
        });
        setPreview(p);
        if (lim === null) setLimit(p.limit_net);
        setError(null);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [legs, duration, spec],
  );

  // Preview on open, and again (debounced) when anything on the ticket changes.
  useEffect(() => {
    const t = window.setTimeout(() => run(limit), limit === null ? 0 : 300);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [legs, limit, duration]);

  const live = spec.account === "live";
  const liveBlocks = preview?.live_blocks ?? [];
  const canPlace =
    !!preview && preview.errors.length === 0 && (!live || liveBlocks.length === 0) && !busy && limit !== null;
  // A live order takes two clicks: Place, then Send to Schwab.
  const [confirming, setConfirming] = useState(false);
  useEffect(() => setConfirming(false), [legs, limit, duration]);
  const place = async () => {
    if (!canPlace) return;
    if (live && !confirming) {
      setConfirming(true);
      return;
    }
    setConfirming(false);
    setBusy(true);
    try {
      const o = await postJson<SimOrder>("/api/orders", {
        account: spec.account,
        intent: spec.intent,
        trade_id: spec.tradeId,
        legs,
        limit_net: limit,
        duration,
        automanage: spec.intent === "open" && manage,
      });
      setPlaced(o);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const nudge = (d: number) => setLimit((v) => (v === null ? v : Math.round((v + d) * 100) / 100));
  useEffect(() => {
    panel.current?.focus();
  }, []);
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") onClose();
    else if ((e.ctrlKey || e.metaKey) && e.key === "Enter") place();
    else if (e.key === "ArrowUp" && (e.target as HTMLElement).tagName !== "SELECT") {
      e.preventDefault();
      nudge(0.01);
    } else if (e.key === "ArrowDown" && (e.target as HTMLElement).tagName !== "SELECT") {
      e.preventDefault();
      nudge(-0.01);
    }
  };

  const p = preview?.prices;
  const sp = preview?.schwab_preview;
  const credit = (limit ?? 0) > 0;

  return (
    <div className="ticket-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="ticket" role="dialog" aria-modal="true" aria-label={spec.title} tabIndex={-1} ref={panel} onKeyDown={onKey}>
        <div className="ticket-head">
          <strong>{spec.title}</strong>
          <span className={`badge ${spec.account === "sim" ? "sim" : "live"}`}>{spec.account === "sim" ? "SIMULATED" : "SCHWAB"}</span>
          <button type="button" className="x" aria-label="Close ticket" onClick={() => onClose()}>
            ×
          </button>
        </div>

        {placed ? (
          <div className="ticket-done">
            <p>
              {live ? "Schwab order" : "Simulated order"} #{placed.id} <b>{live ? "sent" : placed.status.toLowerCase()}</b>
              {placed.status === "FILLED" ? ` at ${netLabel(placed.fill_net)}` : ` at a limit of ${netLabel(placed.limit_net)}`}.
              {placed.replaces?.length ? ` It replaced order #${placed.replaces.join(", #")}.` : ""}
            </p>
            {live ? (
              <p className="dim-text">
                Follow it on the Activity page, which reads Schwab's orders every 10 seconds while it works.
                {spec.intent === "open" && manage ? " Automanage switches on for the trade once it fills." : ""}
              </p>
            ) : placed.status === "WORKING" ? (
              <p className="dim-text">It fills when the mid price reaches the limit. See it on the Activity page.</p>
            ) : null}
            <button type="button" className="primary" onClick={() => onClose(placed)}>
              Done
            </button>
          </div>
        ) : (
          <>
            {spec.roll ? (
              <div className="roll-pick">
                <label>
                  Roll to
                  <select value={rollExp ?? ""} onChange={(e) => setRollExp(e.target.value)}>
                    {exps.map((e) => (
                      <option key={e.date} value={e.date}>
                        {shortDate(e.date)} · {e.dte}d{e.monthly ? " · monthly" : ""}
                      </option>
                    ))}
                  </select>
                </label>
                {spec.roll.legs
                  .filter((l) => l.kind === "OPTION")
                  .map((l) => (
                    <span key={l.symbol} className="shift">
                      {l.put_call === "P" ? "Put" : "Call"} {strike(l.strike)}
                      <button type="button" aria-label="Lower strike" onClick={() => setShift({ ...shift, [l.symbol]: (shift[l.symbol] ?? 0) - 1 })}>−</button>
                      <button type="button" aria-label="Higher strike" onClick={() => setShift({ ...shift, [l.symbol]: (shift[l.symbol] ?? 0) + 1 })}>+</button>
                    </span>
                  ))}
              </div>
            ) : null}

            <table className="ticket-legs">
              <tbody>
                {(preview?.legs ?? []).map((l) => (
                  <tr key={l.symbol}>
                    <td className={`l instr ${l.instruction.startsWith("SELL") ? "neg" : "pos"}`}>{l.instruction.replace(/_/g, " ").toLowerCase()}</td>
                    <td>{l.quantity}</td>
                    <td className="l">
                      {shortDate(l.expiration)} {strike(l.strike)} {l.put_call === "P" ? "Put" : "Call"}
                    </td>
                    <td className="sub">
                      {num(l.bid, 2)} × {num(l.ask, 2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="price-row">
              <div className="presets">
                {(["natural", "mid", "far"] as const).map((k) => (
                  <button key={k} type="button" disabled={!p || p[k] === null} onClick={() => p && setLimit(p[k])}>
                    {k === "natural" ? "Natural" : k === "mid" ? "Mid" : "Far"} <span className="sub">{netLabel(p ? p[k] : null)}</span>
                  </button>
                ))}
              </div>
              <div className="limit">
                <label htmlFor="limit">Limit {credit ? "credit" : "debit"}</label>
                <div className="limit-ctl">
                  <button type="button" aria-label="Worse by one cent" onClick={() => nudge(-0.01)}>−</button>
                  <input
                    id="limit"
                    type="number"
                    step="0.01"
                    value={limit === null ? "" : Math.abs(limit).toFixed(2)}
                    onChange={(e) => {
                      const v = Number(e.target.value);
                      if (!Number.isNaN(v)) setLimit((limit ?? 0) < 0 ? -v : v);
                    }}
                  />
                  <button type="button" aria-label="Better by one cent" onClick={() => nudge(0.01)}>+</button>
                </div>
                {p && p.natural !== null && p.far !== null && p.far > p.natural ? (
                  <input
                    type="range"
                    min={p.natural}
                    max={p.far}
                    step={0.01}
                    value={limit ?? p.natural}
                    onChange={(e) => setLimit(Number(e.target.value))}
                    aria-label="Limit between natural and far"
                  />
                ) : null}
              </div>
            </div>
            <div className="opts">
              <label>
                <input type="radio" checked={duration === "DAY"} onChange={() => setDuration("DAY")} /> Day
              </label>
              <label>
                <input type="radio" checked={duration === "GTC"} onChange={() => setDuration("GTC")} /> GTC
              </label>
              {spec.intent === "open" ? (
                <label
                  title={
                    profitLevel
                      ? `The Profit target rule is at ${profitLevel} (Settings, Automation)` +
                        (profitLevel === "auto" ? ": it keeps the GTC order resting itself." : ".")
                      : undefined
                  }
                >
                  <input
                    type="checkbox"
                    checked={manage}
                    onChange={(e) => setManage(e.target.checked)}
                  />{" "}
                  Automanage on fill
                  {manage && profitLevel && profitLevel !== "auto" ? (
                    <span className="dim-text"> (profit target: {profitLevel})</span>
                  ) : null}
                </label>
              ) : null}
            </div>

            {preview ? (
              <div className="ticket-sum">
                <span>
                  <span className="lbl">Total</span> {money(Math.abs((limit ?? 0) * 100 * preview.units))} {credit ? "credit" : "debit"}
                </span>
                <span>
                  <span className="lbl">Commission</span> {money(preview.commission, 2)}
                </span>
                {preview.bp_effect !== null ? (
                  <span>
                    <span className="lbl">BP effect{preview.defined_risk ? "" : " ≈"}</span> {money(preview.bp_effect)}
                  </span>
                ) : null}
                {preview.bp_used_after !== null ? (
                  <span>
                    <span className="lbl">BP used after</span> {pct(preview.bp_used_after, 1)}
                  </span>
                ) : null}
              </div>
            ) : null}

            {preview?.errors.map((e) => (
              <div key={e} className="msg bad">{e}</div>
            ))}
            {preview?.warnings.map((w) => (
              <div key={w} className="msg warn">{w}</div>
            ))}
            {sp ? (
              <div className={`msg ${sp.error || !sp.ok ? "warn" : "ok"}`}>
                {sp.simulated
                  ? "Demo mode: Schwab preview skipped."
                  : sp.error
                    ? `Schwab preview failed: ${sp.error}`
                    : `Schwab preview: ${sp.ok ? "accepted" : "rejected"}${
                        sp.fields.projectedCommission !== undefined ? ` · commission ${money(sp.fields.projectedCommission, 2)}` : ""
                      }${sp.fields.projectedBuyingPower !== undefined ? ` · buying power after ${money(sp.fields.projectedBuyingPower)}` : ""}`}
                {sp.messages?.map((m) => (
                  <div key={m.text} className="sub">
                    {m.level}: {m.text}
                  </div>
                ))}
              </div>
            ) : null}
            {live
              ? liveBlocks.map((b) => (
                  <div key={b} className="msg warn">
                    Live: {b}
                  </div>
                ))
              : null}
            {error ? <div className="msg bad">{error}</div> : null}

            {preview?.schwab_json ? (
              <details className="json">
                <summary>Schwab order JSON</summary>
                <pre>{JSON.stringify(preview.schwab_json, null, 2)}</pre>
              </details>
            ) : null}

            <div className="ticket-actions">
              <button type="button" onClick={() => onClose()}>
                Cancel
              </button>
              {!live ? (
                <button type="button" className="primary" disabled={!canPlace} onClick={place} title="Ctrl+Enter">
                  {busy ? "Checking…" : "Place simulated order"}
                </button>
              ) : liveBlocks.length ? (
                <span className="dim-text">Preview only: see why above.</span>
              ) : (
                <button
                  type="button"
                  className={confirming ? "danger" : "primary"}
                  disabled={!canPlace}
                  onClick={place}
                  title="Ctrl+Enter"
                >
                  {busy ? "Sending…" : confirming ? `Send to Schwab: ${netLabel(limit)}` : "Place live order"}
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
