import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getJson, postJson } from "./api";
import PayoffChart from "./PayoffChart";
import { money, num, pct, shortDate, signed, strike } from "./format";
import type { Chain, Expiration, OrderLeg, Preview, ProposalBody, SimOrder, StrategyView } from "./types";

export interface TicketSpec {
  title: string;
  account: string;
  intent: "open" | "close" | "roll" | "adjust";
  underlying: string;
  tradeId: number | string | null;
  legs: OrderLeg[];
  /** For rolls: the strategy being rolled, so the new expiration can be chosen. */
  roll?: StrategyView;
  /** Opened from a proposal's Review: placing it settles that proposal. */
  proposalId?: string;
  /** A proposal's own Automanage choice and view, instead of the Settings default. */
  automanage?: boolean;
  view?: string | null;
  /** An earnings play from the scanner: automanage closes it after the report. */
  play?: string | null;
  /** A working order this ticket changes: placing sends the new one in its place. */
  replaces?: number | string;
  /** Start from this limit and duration (a working order's) instead of the mid and Day. */
  limit?: number;
  duration?: "DAY" | "GTC";
  /** Called when the ticket closes, with the order if one was placed. */
  onClosed?: (placed?: SimOrder) => void;
}

/** A proposed order (from automanage or the scanner) as a ticket, to read in full first. */
export function proposalSpec(
  p: { id?: string | null; underlying: string; strategy: string; trade_id?: string | null; automanage?: boolean; view?: string | null; play?: string | null },
  body: ProposalBody,
  account: string,
): TicketSpec {
  const intent = (["open", "close", "roll", "adjust"].includes(body.intent) ? body.intent : "open") as TicketSpec["intent"];
  const verb = { open: "Open", close: "Close", roll: "Roll", adjust: "Adjust" }[intent];
  return {
    title: `${verb} ${p.underlying} ${p.strategy}`,
    account,
    intent,
    underlying: p.underlying,
    tradeId: !p.trade_id || p.trade_id.startsWith("new:") ? null : p.trade_id,
    legs: body.legs.map((l) => ({ symbol: l.symbol, quantity: l.quantity, effect: l.effect === "close" ? "close" : "open" })),
    proposalId: p.id ?? undefined,
    automanage: p.automanage,
    view: p.view,
    play: p.play,
  };
}

/** The same opening order in another account, sized to its net liq (from /api/orders/copies). */
interface CopyRow {
  account: string;
  name: string;
  net_liq: number | null;
  scaled: number | null;
  units: number | null;
  legs: OrderLeg[] | null;
  preview: Preview | null;
  errors: string[];
}

/** What a copy placed, or why it wasn't. */
interface CopyResult {
  name: string;
  live: boolean;
  order?: SimOrder;
  error?: string;
}

/** Why a copy can't be placed: its own guards, its live gate, or no size. */
function copyStops(c: CopyRow): string[] {
  return [...c.errors, ...(c.preview?.errors ?? []), ...(c.account !== "sim" ? (c.preview?.live_blocks ?? []) : [])];
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
export function closeSpec(st: StrategyView, underlying: string, account: string, side: Side = null): TicketSpec {
  return {
    title: `Close ${underlying} ${st.name}${sideName(side)}`,
    account,
    intent: "close",
    underlying,
    tradeId: st.trade_id,
    legs: sideLegs(st, side).map((l) => ({ symbol: l.symbol, quantity: -l.quantity, effect: "close" as const })),
  };
}

/** Close the shares of a strategy (Schwab account only): an order of their own. */
export function closeStockSpec(st: StrategyView, underlying: string, account: string): TicketSpec {
  const shares = st.legs.filter((l) => l.kind === "EQUITY");
  return {
    title: `Close ${underlying} shares`,
    account,
    intent: "close",
    underlying,
    tradeId: st.trade_id,
    legs: shares.map((l) => ({ symbol: l.symbol, quantity: -l.quantity, effect: "close" as const })),
  };
}

/** Roll the option legs of a strategy, or of one side of it (closing and opening legs count toward the limit). */
export function rollSpec(st: StrategyView, underlying: string, account: string, side: Side = null): TicketSpec {
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
  if (all === 0) return { close: [], roll: [] }; // shares only
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
  const [limit, setLimit] = useState<number | null>(spec.limit ?? null);
  const [duration, setDuration] = useState<"DAY" | "GTC">(spec.duration ?? "DAY");
  // "Automanage on fill": the default comes from Settings, Automation. The Profit target rule
  // then closes the trade at its target (50%, 25% for straddles), at the level set there.
  const [manage, setManage] = useState(false);
  // The new trade's view for automanage's rolls; "portfolio" follows the delta bias.
  const [tradeView, setTradeView] = useState(spec.view ?? "portfolio");
  const [profitLevel, setProfitLevel] = useState<string | null>(null);
  useEffect(() => {
    if (spec.intent !== "open") return;
    getJson<{ settings: { ticket_automanage?: boolean; levels: Record<string, string> } }>(
      `/api/automation?account=${spec.account}`,
    )
      .then((v) => {
        setProfitLevel(v.settings.levels.profit ?? null);
        setManage(spec.automanage ?? !!v.settings.ticket_automanage);
      })
      .catch(() => undefined);
  }, [spec.intent, spec.account, spec.automanage]);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [placed, setPlaced] = useState<SimOrder | null>(null);
  // Copies of an opening order to the other accounts, each sized to its own net liq.
  const canCopy = spec.intent === "open" && !spec.roll && spec.replaces === undefined;
  const [copies, setCopies] = useState<CopyRow[]>([]);
  const [copyOn, setCopyOn] = useState<Record<string, boolean>>({});
  const [copyUnits, setCopyUnits] = useState<Record<string, number>>({});
  const [copied, setCopied] = useState<CopyResult[]>([]);
  // Closed until wanted: most orders go to one account.
  const [copiesOpen, setCopiesOpen] = useState(false);
  const panel = useRef<HTMLDivElement>(null);

  // Roll: choose the new expiration and strikes.
  const [exps, setExps] = useState<Expiration[]>([]);
  const [rollExp, setRollExp] = useState<string | null>(null);
  const [chain, setChain] = useState<Chain | null>(null);
  // The strike each leg rolls to, as chosen on the ticket; unset legs keep their strike.
  const [pick, setPick] = useState<Record<string, number>>({});
  const [rollBuilt, setRollBuilt] = useState(false);
  const current = spec.roll?.numbers.expiration ?? null;

  useEffect(() => {
    if (!spec.roll) return;
    getJson<{ expirations: Expiration[] }>(`/api/trade/${encodeURIComponent(spec.underlying)}/expirations`)
      .then((r) => {
        // Every expiration, the current and nearer ones too: rolling up or down in the same
        // expiration, or in to a nearer one, is a roll as well. The default is still out.
        setExps(r.expirations);
        const later = r.expirations.filter((e) => !current || e.date > current);
        const monthlies = later.filter((e) => e.monthly);
        const pool = monthlies.length ? monthlies : later;
        if (pool.length) setRollExp(pool.reduce((a, b) => (Math.abs(b.dte - 45) < Math.abs(a.dte - 45) ? b : a)).date);
        else if (current) setRollExp(current);
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
  // The new strike for a leg: the one chosen on the ticket, else the same strike; the nearest
  // listed in the new expiration. null until its chain is in.
  const newStrike = useCallback(
    (symbol: string, k: number): number | null => {
      if (!strikes.length) return null;
      const want = pick[symbol] ?? k;
      return strikes.reduce((a, b) => (Math.abs(b - want) < Math.abs(a - want) ? b : a));
    },
    [strikes, pick],
  );

  // Each strike's contract in the new expiration, for its delta.
  const contract = useCallback(
    (pc: string | null, k: number | null) => {
      const r = chain?.rows.find((x) => x.strike === k);
      return (pc === "P" ? r?.put : r?.call) ?? null;
    },
    [chain],
  );
  // The trade's delta in shares now, and after the roll: the rolled legs swapped for the new
  // ones at their delta in the new expiration. Other legs (the side not rolled, shares) stay.
  const rollDelta = useMemo(() => {
    const r = spec.roll;
    if (!r || r.numbers.delta === null || !chain) return null;
    let after = r.numbers.delta;
    for (const l of r.legs) {
      if (l.kind !== "OPTION" || l.strike === null) continue;
      const to = newStrike(l.symbol, l.strike);
      if (rollExp === l.expiration && to === l.strike) continue; // not moved: its delta stays
      const d = contract(l.put_call, to)?.delta;
      if (l.delta === null || d === null || d === undefined) return null;
      after += l.quantity * 100 * (d - l.delta);
    }
    return { now: r.numbers.delta, after };
  }, [spec.roll, chain, contract, newStrike, rollExp]);

  useEffect(() => {
    if (!spec.roll || !rollExp || !strikes.length) return;
    const opening: OrderLeg[] = spec.roll.legs
      .filter((l) => l.kind === "OPTION" && l.strike !== null)
      .map((l) => ({
        symbol: occ(spec.underlying, rollExp, l.put_call!, newStrike(l.symbol, l.strike!)!),
        quantity: l.quantity,
        effect: "open" as const,
      }));
    // A leg left at its own expiration and strike is not part of the roll: closing and
    // reopening the same contract would only cost commission, and is one contract twice.
    const kept = new Set(opening.map((o) => o.symbol).filter((sym) => spec.legs.some((c) => c.symbol === sym)));
    setLegs([...spec.legs, ...opening].filter((l) => !kept.has(l.symbol)));
    setRollBuilt(true);
    setLimit(null);
  }, [spec.roll, spec.legs, spec.underlying, rollExp, strikes, newStrike]);

  const run = useCallback(
    async (lim: number | null) => {
      if (spec.roll && !rollBuilt) return; // roll legs not built yet
      if (!legs.length) {
        setPreview(null);
        setError("Nothing to roll: pick a different expiration or strike");
        return;
      }
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
    [legs, duration, spec, rollBuilt],
  );

  // Preview on open, and again (debounced) when anything on the ticket changes.
  useEffect(() => {
    const t = window.setTimeout(() => run(limit), limit === null ? 0 : 300);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [legs, limit, duration]);

  // Copies: sized and checked again whenever the order, or which copies are on, changes.
  useEffect(() => {
    if (!canCopy || limit === null) return;
    const t = window.setTimeout(() => {
      postJson<{ copies: CopyRow[] }>("/api/orders/copies", {
        order: { account: spec.account, intent: spec.intent, legs, limit_net: limit, duration },
        preview: Object.keys(copyOn).filter((k) => copyOn[k]),
        units: copyUnits,
      })
        .then((r) => setCopies(r.copies))
        .catch(() => setCopies([]));
    }, 400);
    return () => window.clearTimeout(t);
  }, [canCopy, spec.account, spec.intent, legs, limit, duration, copyOn, copyUnits]);
  // The copies that go with the order: on, previewed, and nothing stopping them.
  const toCopy = copies.filter((c) => copyOn[c.account] && c.preview && c.legs && copyStops(c).length === 0);
  const liveCopies = toCopy.filter((c) => c.account !== "sim");
  const copyText = toCopy.map((c) => `${c.name} ${c.units}`).join(", ");

  const live = spec.account !== "sim";
  const liveBlocks = preview?.live_blocks ?? [];
  // Everything that stops the order, in one list: the guards, the live gate, a failed send.
  const stops = [...(preview?.errors ?? []), ...(live ? liveBlocks : []), ...(error ? [error] : [])];
  const canPlace =
    !!preview && preview.errors.length === 0 && (!live || liveBlocks.length === 0) && !busy && limit !== null;
  // A live order takes two clicks: Place, then Send to Schwab. So does any live copy.
  const [confirming, setConfirming] = useState(false);
  useEffect(() => setConfirming(false), [legs, limit, duration, copyOn, copyUnits]);
  const needConfirm = live || liveCopies.length > 0;
  // A working order opened here to replace: it can be cancelled from the ticket too. Live
  // takes two taps, as placing does.
  const [cancelConfirm, setCancelConfirm] = useState(false);
  const cancelOrder = async () => {
    if (spec.replaces === undefined) return;
    if (live && !cancelConfirm) {
      setCancelConfirm(true);
      return;
    }
    setCancelConfirm(false);
    setBusy(true);
    try {
      await postJson(`/api/orders/${encodeURIComponent(String(spec.replaces))}/cancel?account=${spec.account}`, {});
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const place = async () => {
    if (!canPlace) return;
    if (needConfirm && !confirming) {
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
        view: spec.intent === "open" && manage && tradeView !== "portfolio" ? tradeView : null,
        play: spec.intent === "open" && manage ? (spec.play ?? null) : null,
        proposal_id: spec.proposalId ?? null,
        replaces: spec.replaces ?? null,
      });
      // Then each copy, in its own account, with the same price, duration and automanage.
      const results: CopyResult[] = [];
      for (const c of toCopy) {
        try {
          const co = await postJson<SimOrder>("/api/orders", {
            account: c.account,
            intent: "open",
            legs: c.legs,
            limit_net: limit,
            duration,
            automanage: manage,
            view: manage && tradeView !== "portfolio" ? tradeView : null,
            play: manage ? (spec.play ?? null) : null,
          });
          results.push({ name: c.name, live: c.account !== "sim", order: co });
        } catch (e) {
          results.push({ name: c.name, live: c.account !== "sim", error: (e as Error).message });
        }
      }
      setCopied(results);
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
                Follow it on the History page, which reads Schwab's orders every 10 seconds while it works.
                {spec.intent === "open" && manage ? " Automanage switches on for the trade once it fills." : ""}
              </p>
            ) : placed.status === "WORKING" ? (
              <p className="dim-text">It fills when the mid price reaches the limit. See it on the History page.</p>
            ) : null}
            {copied.length ? (
              <ul className="copied">
                {copied.map((c) => (
                  <li key={c.name} className={c.error ? "neg" : ""}>
                    {c.name}:{" "}
                    {c.order
                      ? `${c.live ? "Schwab order" : "simulated order"} #${c.order.id} ${c.live ? "sent" : c.order.status.toLowerCase()}`
                      : `not placed: ${c.error}`}
                  </li>
                ))}
              </ul>
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
                        {e.date === current ? " · current" : current && e.date < current ? " · in" : ""}
                      </option>
                    ))}
                  </select>
                </label>
                {spec.roll.legs
                  .filter((l) => l.kind === "OPTION")
                  .map((l) => {
                    const to = l.strike !== null ? newStrike(l.symbol, l.strike) : null;
                    const moved = to !== null && l.strike !== null && to !== l.strike;
                    return (
                      <span key={l.symbol} className="shift">
                        {l.put_call === "P" ? "Put" : "Call"} {strike(l.strike)}
                        {" → "}
                        <select
                          className={`shift-to ${moved ? (to! < l.strike! ? "down" : "up") : ""}`}
                          aria-label={`New strike for the ${l.put_call === "P" ? "put" : "call"} ${strike(l.strike)}`}
                          title="The strike this leg rolls to in the chosen expiration"
                          value={to ?? ""}
                          disabled={to === null}
                          onChange={(e) => setPick({ ...pick, [l.symbol]: Number(e.target.value) })}
                        >
                          {to === null ? <option value="">…</option> : null}
                          {strikes.map((k) => (
                            <option key={k} value={k}>
                              {strike(k)} · Δ {num(contract(l.put_call, k)?.delta ?? null, 2)}
                              {k === l.strike ? " · same" : ""}
                            </option>
                          ))}
                        </select>
                      </span>
                    );
                  })}
                <span
                  className="roll-delta"
                  title="The trade's delta in shares, now and after this roll, at the new legs' deltas in the chosen expiration"
                >
                  Δ {signed(rollDelta?.now ?? spec.roll.numbers.delta, 0)} → <b>{signed(rollDelta?.after ?? null, 0)}</b>
                  <span className="sub"> after roll</span>
                </span>
              </div>
            ) : null}

            <table className="ticket-legs">
              <tbody>
                {(preview?.legs ?? []).map((l) => (
                  <tr key={l.symbol}>
                    <td className={`l instr ${l.instruction.startsWith("SELL") ? "neg" : "pos"}`}>
                      {l.instruction.startsWith("SELL") ? "Sell" : "Buy"} {l.quantity}
                      <span className="sub"> to {l.instruction.endsWith("CLOSE") ? "close" : "open"}</span>
                    </td>
                    <td className="l">
                      {l.put_call ? `${shortDate(l.expiration)} ${strike(l.strike)} ${l.put_call === "P" ? "Put" : "Call"}` : `${l.symbol} shares`}
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
              {spec.intent === "open" && manage ? (
                <select
                  className="view-select"
                  value={tradeView}
                  onChange={(e) => setTradeView(e.target.value)}
                  title="The trade's view: a defensive roll leaves it leaning this way. Portfolio follows the delta bias in Settings."
                  aria-label="The new trade's view"
                >
                  <option value="portfolio">View: portfolio</option>
                  <option value="neutral">View: neutral</option>
                  <option value="bullish">View: bullish</option>
                  <option value="bearish">View: bearish</option>
          <option value="would_own">View: would own (cash-secured)</option>
                </select>
              ) : null}
            </div>

            {canCopy && copies.length ? (
              <div className="copies">
                <button
                  type="button"
                  className="link copies-head"
                  aria-expanded={copiesOpen}
                  onClick={() => setCopiesOpen((v) => !v)}
                >
                  {copiesOpen ? "▾" : "▸"} Also place in other accounts
                  {toCopy.length ? ` (${toCopy.length})` : ""}
                  {copiesOpen ? <span className="dim-text"> sized to each account's net liq</span> : null}
                </button>
                {(copiesOpen ? copies : copies.filter((c) => copyOn[c.account])).map((c) => {
                  const on = !!copyOn[c.account];
                  const stops = on ? copyStops(c) : [];
                  return (
                    <div key={c.account} className={`copy${stops.length ? " blocked" : ""}`}>
                      <label>
                        <input
                          type="checkbox"
                          checked={on}
                          onChange={(e) => setCopyOn((m) => ({ ...m, [c.account]: e.target.checked }))}
                        />{" "}
                        {c.name}
                      </label>
                      <input
                        type="number"
                        className="copy-units"
                        aria-label={`Spreads in ${c.name}`}
                        min={1}
                        max={1000}
                        value={c.units ?? ""}
                        disabled={!on}
                        onChange={(e) => {
                          const n = Math.floor(Number(e.target.value));
                          if (n >= 1 && n <= 1000) setCopyUnits((m) => ({ ...m, [c.account]: n }));
                        }}
                      />
                      <span className="dim-text">
                        {c.net_liq !== null ? `net liq ${money(c.net_liq)}` : ""}
                        {c.scaled !== null && c.units !== c.scaled ? ` · scaled ${c.scaled}` : ""}
                      </span>
                      {on && !stops.length ? (
                        <span className="copy-status">
                          {c.preview ? (c.preview.bp_effect !== null ? `ready · BP ${money(c.preview.bp_effect)}` : "ready") : "checking…"}
                        </span>
                      ) : null}
                      {stops.length ? (
                        <ul>
                          {stops.map((x) => (
                            <li key={x}>{x}</li>
                          ))}
                        </ul>
                      ) : null}
                      {on && c.preview?.warnings.length ? (
                        <ul className="warn-text">
                          {c.preview.warnings.map((w) => (
                            <li key={w}>{w}</li>
                          ))}
                        </ul>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            ) : null}

            {preview ? (
              <div className="ticket-sum">
                <div>
                  <div className="lbl">Total {credit ? "credit" : "debit"}</div>
                  <div className="val">{money(Math.abs((limit ?? 0) * (preview.multiplier ?? 100) * preview.units))}</div>
                </div>
                <div>
                  <div className="lbl">Commission</div>
                  <div className="val">{money(preview.commission, 2)}</div>
                </div>
                {preview.bp_effect !== null ? (
                  <div>
                    <div className="lbl">BP effect{preview.defined_risk ? "" : " ≈"}</div>
                    <div className="val">{money(preview.bp_effect)}</div>
                  </div>
                ) : null}
                {preview.bp_used_after !== null ? (
                  <div>
                    <div className="lbl">BP used after</div>
                    <div className="val">{pct(preview.bp_used_after, 1)}</div>
                  </div>
                ) : null}
                {preview.pop !== null && preview.pop !== undefined ? (
                  <div
                    title="The chance the trade is worth more than nothing at expiration, at this limit, from implied volatility"
                  >
                    <div className="lbl">Prob. of profit</div>
                    <div className="val">
                      {pct(preview.pop)}
                      {preview.pop >= 0.8 ? (
                        <svg className="pop-seal" viewBox="0 0 24 24" role="img" aria-label="80% or more">
                          <title>80% or more</title>
                          <polygon points="12.00,1.00 14.43,2.92 17.50,2.47 18.65,5.35 21.53,6.50 21.08,9.57 23.00,12.00 21.08,14.43 21.53,17.50 18.65,18.65 17.50,21.53 14.43,21.08 12.00,23.00 9.57,21.08 6.50,21.53 5.35,18.65 2.47,17.50 2.92,14.43 1.00,12.00 2.92,9.57 2.47,6.50 5.35,5.35 6.50,2.47 9.57,2.92" />
                          <circle cx="12" cy="12" r="7.6" />
                          <path d="M8.2 12.3l2.5 2.5 5.1-5.3" />
                        </svg>
                      ) : null}
                    </div>
                    {preview.breakevens?.length ? (
                      <div className="sub">break-even {preview.breakevens.map((b) => num(b, 2)).join(" / ")}</div>
                    ) : null}
                  </div>
                ) : null}
              </div>
            ) : null}

            {stops.length ? (
              <div className="msg bad">
                <div className="msg-title">{live ? "Can't send to Schwab" : "Can't place this order"}</div>
                <ul>
                  {stops.map((x) => (
                    <li key={x}>{x}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {preview?.warnings.length ? (
              <div className="msg warn">
                <div className="msg-title">Check before placing</div>
                <ul>
                  {preview.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {/* Schwab's preview speaks up only when it fails; accepted is the normal case. */}
            {sp && (sp.error || !sp.ok) ? (
              <div className={`ticket-status ${sp.error || !sp.ok ? (live ? "bad" : "warn") : "ok"}`}>
                {sp.error
                    ? `Schwab preview failed: ${sp.error}`
                    : `Schwab preview ${sp.ok ? "accepted" : "rejected"}${
                        sp.fields.projectedCommission !== undefined ? ` · commission ${money(sp.fields.projectedCommission, 2)}` : ""
                      }${sp.fields.projectedBuyingPower !== undefined ? ` · buying power after ${money(sp.fields.projectedBuyingPower)}` : ""}`}
                {sp.messages?.map((m) => (
                  <div key={m.text} className="sub">
                    {m.level}: {m.text}
                  </div>
                ))}
              </div>
            ) : null}

            {preview?.schwab_json && (stops.length || (sp && (sp.error || !sp.ok))) ? (
              <details className="json">
                <summary>Schwab order JSON</summary>
                <pre>{JSON.stringify(preview.schwab_json, null, 2)}</pre>
              </details>
            ) : null}

            <div className="ticket-actions">
              <button type="button" onClick={() => onClose()}>
                {spec.replaces !== undefined ? "Close" : "Cancel"}
              </button>
              {spec.replaces !== undefined ? (
                <button
                  type="button"
                  className={cancelConfirm ? "danger" : ""}
                  disabled={busy}
                  onClick={cancelOrder}
                  title="Cancel the working order; the window closes"
                >
                  {cancelConfirm
                    ? `Cancel #${spec.replaces}${live ? " at Schwab" : ""}?`
                    : "Cancel order"}
                </button>
              ) : null}
              {!live ? (
                <button
                  type="button"
                  className={confirming ? "danger" : "primary"}
                  disabled={!canPlace}
                  onClick={place}
                  title="Ctrl+Enter"
                >
                  {busy
                    ? "Checking…"
                    : confirming
                      ? `Place, and send copies to Schwab: ${copyText}`
                      : toCopy.length
                        ? `Place simulated order + ${toCopy.length} ${toCopy.length === 1 ? "copy" : "copies"}`
                        : spec.replaces !== undefined
                          ? `Replace order #${spec.replaces}`
                          : "Place simulated order"}
                </button>
              ) : liveBlocks.length ? (
                <span className="dim-text">Preview only: see the red box.</span>
              ) : (
                <button
                  type="button"
                  className={confirming ? "danger" : "primary"}
                  disabled={!canPlace}
                  onClick={place}
                  title="Ctrl+Enter"
                >
                  {busy
                    ? "Sending…"
                    : confirming
                      ? `${spec.replaces !== undefined ? `Replace #${spec.replaces} at Schwab` : "Send to Schwab"}: ${netLabel(limit)}${toCopy.length ? ` · copies: ${copyText}` : ""}`
                      : toCopy.length
                        ? `Place live order + ${toCopy.length} ${toCopy.length === 1 ? "copy" : "copies"}`
                        : spec.replaces !== undefined
                          ? `Replace order #${spec.replaces}`
                          : "Place live order"}
                </button>
              )}
            </div>
            {preview?.payoff && preview.spot ? <PayoffChart a={preview.payoff} spot={preview.spot} /> : null}
          </>
        )}
      </div>
    </div>
  );
}
