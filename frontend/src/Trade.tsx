import { lazy, Suspense, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { AccountContext, TicketContext } from "./context";
import { MAX_LEGS } from "./OrderTicket";
import { getJson, postJson } from "./api";
import { money, num, pct, shortDate, signed, strike, tone } from "./format";
import PayoffChart from "./PayoffChart";
import type { Analysis, BuildLeg, Chain, ChainRow, Contract, Expiration } from "./types";

// The chart library is large: load it only when the Trade screen opens.
const PriceChart = lazy(() => import("./PriceChart"));

type Side = "call" | "put";

function legFrom(c: Contract, row: ChainRow, side: Side, exp: string, qty: number): BuildLeg {
  return {
    symbol: c.symbol,
    put_call: side === "put" ? "P" : "C",
    strike: row.strike,
    expiration: exp,
    quantity: qty,
    price: c.mark ?? ((c.bid ?? 0) + (c.ask ?? 0)) / 2,
    iv: c.iv,
    delta: c.delta,
    theta: c.theta,
    vega: c.vega,
  };
}

function byDelta(rows: ChainRow[], side: Side, target: number): ChainRow | null {
  let best: ChainRow | null = null;
  for (const r of rows) {
    const d = r[side]?.delta;
    if (d === null || d === undefined) continue;
    if (!best || Math.abs(Math.abs(d) - target) < Math.abs(Math.abs(best[side]!.delta!) - target)) best = r;
  }
  return best;
}

function wing(rows: ChainRow[], from: ChainRow, side: Side, width: number): ChainRow | null {
  // Further out of the money by about `width` dollars.
  const want = side === "put" ? from.strike - width : from.strike + width;
  const pool = rows.filter((r) => r[side] && (side === "put" ? r.strike < from.strike : r.strike > from.strike));
  if (!pool.length) return null;
  return pool.reduce((a, b) => (Math.abs(b.strike - want) < Math.abs(a.strike - want) ? b : a));
}

interface Params {
  strangle: number;
  vertical: number;
  condor: number;
  width: number;
}

type Builder = {
  name: string;
  title?: string;
  build: (rows: ChainRow[], exp: string, p: Params, spot: number, shares: number) => BuildLeg[] | null;
};

const BUILDERS: Builder[] = [
  {
    name: "Strangle",
    build: (rows, exp, p) => {
      const sp = byDelta(rows, "put", p.strangle);
      const sc = byDelta(rows, "call", p.strangle);
      if (!sp || !sc) return null;
      return [legFrom(sp.put!, sp, "put", exp, -1), legFrom(sc.call!, sc, "call", exp, -1)];
    },
  },
  {
    name: "Straddle",
    build: (rows, exp, _p, spot) => {
      const atm = rows.reduce((a, b) => (Math.abs(b.strike - spot) < Math.abs(a.strike - spot) ? b : a));
      if (!atm.put || !atm.call) return null;
      return [legFrom(atm.put, atm, "put", exp, -1), legFrom(atm.call, atm, "call", exp, -1)];
    },
  },
  {
    name: "Short put",
    title: "Cash-secured when the account holds the strike's value in cash",
    build: (rows, exp, p) => {
      const sp = byDelta(rows, "put", p.vertical);
      return sp ? [legFrom(sp.put!, sp, "put", exp, -1)] : null;
    },
  },
  {
    name: "Covered call",
    title: "Sells one call at the short delta for every 100 shares this account holds",
    build: (rows, exp, p, _spot, shares) => {
      const n = Math.floor(shares / 100);
      const sc = byDelta(rows, "call", p.vertical);
      if (n < 1 || !sc) return null;
      return [legFrom(sc.call!, sc, "call", exp, -n)];
    },
  },
  {
    name: "Put vertical",
    build: (rows, exp, p) => {
      const sp = byDelta(rows, "put", p.vertical);
      const lp = sp && wing(rows, sp, "put", p.width);
      if (!sp || !lp) return null;
      return [legFrom(sp.put!, sp, "put", exp, -1), legFrom(lp.put!, lp, "put", exp, 1)];
    },
  },
  {
    name: "Call vertical",
    build: (rows, exp, p) => {
      const sc = byDelta(rows, "call", p.vertical);
      const lc = sc && wing(rows, sc, "call", p.width);
      if (!sc || !lc) return null;
      return [legFrom(sc.call!, sc, "call", exp, -1), legFrom(lc.call!, lc, "call", exp, 1)];
    },
  },
  {
    name: "Iron condor",
    build: (rows, exp, p) => {
      const sp = byDelta(rows, "put", p.condor);
      const sc = byDelta(rows, "call", p.condor);
      const lp = sp && wing(rows, sp, "put", p.width);
      const lc = sc && wing(rows, sc, "call", p.width);
      if (!sp || !sc || !lp || !lc) return null;
      return [
        legFrom(lp.put!, lp, "put", exp, 1),
        legFrom(sp.put!, sp, "put", exp, -1),
        legFrom(sc.call!, sc, "call", exp, -1),
        legFrom(lc.call!, lc, "call", exp, 1),
      ];
    },
  },
  {
    name: "Put ratio 1×2",
    title: "Buys one put at the short delta and sells two at the strangle delta (a front ratio)",
    build: (rows, exp, p) => {
      const lp = byDelta(rows, "put", p.vertical);
      const sp = byDelta(rows, "put", p.strangle);
      if (!lp || !sp || sp.strike >= lp.strike) return null;
      return [legFrom(lp.put!, lp, "put", exp, 1), legFrom(sp.put!, sp, "put", exp, -2)];
    },
  },
  {
    name: "Call ratio 1×2",
    title: "Buys one call at the short delta and sells two at the strangle delta (a front ratio)",
    build: (rows, exp, p) => {
      const lc = byDelta(rows, "call", p.vertical);
      const sc = byDelta(rows, "call", p.strangle);
      if (!lc || !sc || sc.strike <= lc.strike) return null;
      return [legFrom(lc.call!, lc, "call", exp, 1), legFrom(sc.call!, sc, "call", exp, -2)];
    },
  },
  {
    name: "Jade lizard",
    build: (rows, exp, p) => {
      const sp = byDelta(rows, "put", p.vertical);
      const sc = byDelta(rows, "call", p.condor);
      const lc = sc && wing(rows, sc, "call", p.width);
      if (!sp || !sc || !lc) return null;
      return [
        legFrom(sp.put!, sp, "put", exp, -1),
        legFrom(sc.call!, sc, "call", exp, -1),
        legFrom(lc.call!, lc, "call", exp, 1),
      ];
    },
  },
];

export default function Trade({ symbol, shares = 0 }: { symbol: string; shares?: number }) {
  const [input, setInput] = useState(symbol);
  const [exps, setExps] = useState<Expiration[]>([]);
  const [exp, setExp] = useState<string | null>(null);
  const [chain, setChain] = useState<Chain | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [legs, setLegs] = useState<BuildLeg[]>([]);
  // Shares already held that a covered call is written against: in the payoff, not the order.
  const [covered, setCovered] = useState(0);
  const coveredNow = legs.length ? covered : 0;
  useEffect(() => {
    if (!legs.length) setCovered(0);
  }, [legs.length]);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [anaError, setAnaError] = useState<string | null>(null);
  const [params, setParams] = useState<Params>({ strangle: 0.16, vertical: 0.3, condor: 0.2, width: 5 });
  const scroller = useRef<HTMLDivElement>(null);
  const openTicket = useContext(TicketContext);
  const account = useContext(AccountContext);
  const centred = useRef<string | null>(null);

  useEffect(() => setInput(symbol), [symbol]);

  // Expirations for the symbol.
  useEffect(() => {
    let stop = false;
    setExps([]);
    setExp(null);
    setChain(null);
    setLegs([]);
    setAnalysis(null);
    setError(null);
    getJson<{ expirations: Expiration[]; default: string }>(`/api/trade/${encodeURIComponent(symbol)}/expirations`)
      .then((r) => {
        if (stop) return;
        setExps(r.expirations);
        setExp(r.default);
      })
      .catch((e) => !stop && setError((e as Error).message));
    return () => {
      stop = true;
    };
  }, [symbol]);

  // Chain for the chosen expiration, refreshed every 10 seconds.
  useEffect(() => {
    if (!exp) return;
    let stop = false;
    const load = async () => {
      try {
        const c = await getJson<Chain>(
          `/api/trade/${encodeURIComponent(symbol)}/chain?expiration=${exp}`,
        );
        if (stop) return;
        setChain(c);
        setError(null);
        // Reprice legs from this expiration at the new mids.
        const bySym = new Map<string, Contract>();
        for (const r of c.rows) for (const k of [r.call, r.put]) if (k) bySym.set(k.symbol, k);
        setLegs((prev) =>
          prev.map((l) => {
            const k = bySym.get(l.symbol);
            return k && k.mark !== null
              ? { ...l, price: k.mark, iv: k.iv, delta: k.delta, theta: k.theta, vega: k.vega }
              : l;
          }),
        );
      } catch (e) {
        if (!stop) setError((e as Error).message);
      }
    };
    load();
    const t = window.setInterval(load, 10000);
    return () => {
      stop = true;
      window.clearInterval(t);
    };
  }, [symbol, exp]);

  // Keep the at-the-money strike in view when a new chain arrives.
  useEffect(() => {
    if (!chain || !scroller.current) return;
    const key = `${chain.symbol}:${chain.expiration}`;
    if (centred.current === key) return;
    centred.current = key;
    const el = scroller.current.querySelector<HTMLElement>("tr.atm");
    if (el) scroller.current.scrollTop = el.offsetTop - scroller.current.clientHeight / 2;
  }, [chain]);

  // Analysis, debounced.
  useEffect(() => {
    if (!legs.length || !chain?.underlying_price) {
      setAnalysis(null);
      return;
    }
    const t = window.setTimeout(async () => {
      try {
        const body = {
          symbol,
          spot: chain.underlying_price,
          vol: chain.atm_iv,
          legs: legs.map(({ symbol: _s, ...l }) => l),
          held_shares: coveredNow,
        };
        let a = await postJson<Analysis>("/api/analyze", body);
        if (coveredNow) {
          // The order is only the calls; the payoff is the covered position, shares at today's
          // price. Cost, buying power and the portfolio after stay the order's own.
          const held = await postJson<Analysis>("/api/analyze", {
            ...body,
            legs: [
              ...body.legs,
              { put_call: null, strike: null, expiration: null, quantity: coveredNow, price: chain.underlying_price },
            ],
          });
          a = {
            ...a,
            curve: held.curve,
            max_profit: held.max_profit,
            max_loss: held.max_loss,
            unlimited_profit: held.unlimited_profit,
            unlimited_loss: held.unlimited_loss,
            breakevens: held.breakevens,
            pop: held.pop,
          };
        }
        setAnalysis(a);
        setAnaError(null);
      } catch (e) {
        setAnaError((e as Error).message);
      }
    }, 250);
    return () => window.clearTimeout(t);
  }, [legs, coveredNow, chain?.underlying_price, chain?.atm_iv, symbol]);

  // Tapping a price selects that contract; tapping it again deselects it. Tapping the other
  // side of a selected contract switches it between buy and sell. Quantity is set in the leg list.
  const toggleLeg = useCallback((leg: BuildLeg) => {
    setLegs((prev) => {
      const i = prev.findIndex((l) => l.symbol === leg.symbol);
      if (i < 0) {
        if (prev.length >= MAX_LEGS) {
          setAnaError(`An order can have at most ${MAX_LEGS} legs (Schwab's limit).`);
          return prev;
        }
        return [...prev, leg];
      }
      const cur = prev[i];
      if (Math.sign(cur.quantity) === Math.sign(leg.quantity)) return prev.filter((_, j) => j !== i);
      return prev.map((l, j) => (j === i ? { ...l, quantity: -l.quantity } : l));
    });
  }, []);

  const spot = chain?.underlying_price ?? null;
  const atmStrike = useMemo(() => {
    if (!chain || spot === null || !chain.rows.length) return null;
    return chain.rows.reduce((a, b) => (Math.abs(b.strike - spot) < Math.abs(a.strike - spot) ? b : a)).strike;
  }, [chain, spot]);
  const inBuild = new Map(legs.map((l) => [l.symbol, l.quantity]));

  const go = (e: React.FormEvent) => {
    e.preventDefault();
    const s = input.trim().toUpperCase();
    if (s) window.location.hash = `#/trade/${encodeURIComponent(s)}`;
  };

  const cell = (c: Contract | null, row: ChainRow, side: Side, kind: "bid" | "ask") => {
    if (!c || c[kind] === null) return <td className="q">–</td>;
    const qty = kind === "bid" ? -1 : 1;
    const held = inBuild.get(c.symbol);
    const selected = held !== undefined && Math.sign(held) === qty;
    return (
      <td className={`q click ${kind}`}>
        <button
          type="button"
          aria-pressed={selected}
          className={selected ? "selected" : ""}
          title={
            selected
              ? `Remove the ${strike(row.strike)} ${side}`
              : `${kind === "bid" ? "Sell" : "Buy"} the ${strike(row.strike)} ${side}`
          }
          onClick={() => exp && toggleLeg(legFrom(c, row, side, exp, qty))}
        >
          {num(c[kind], 2)}
        </button>
      </td>
    );
  };

  return (
    <div className="trade">
      <div className="trade-head">
        <form onSubmit={go} className="sym-form">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            aria-label="Symbol"
            spellCheck={false}
            autoCapitalize="characters"
          />
          <button type="submit">Go</button>
        </form>
        <div className="trade-stats">
          <span className="big">{num(spot, 2)}</span>
          <span>
            IV {num(chain?.atm_iv ?? null, 1)}
            <span className="sub"> at {exp ? shortDate(exp) : "–"}</span>
          </span>
          <span title={chain?.ivr ? `Over ${chain.ivr.days} days` : "No IV history yet"}>
            IVR {chain?.ivr && chain.ivr.rank !== null ? Math.round(chain.ivr.rank) : "–"}
          </span>
          <span>
            Exp. move ±{num(chain?.expected_move ?? null, 2)}
            {chain?.expected_move && spot ? <span className="sub"> {pct(chain.expected_move / spot, 1)}</span> : null}
          </span>
        </div>
      </div>
      {error ? <div className="banner warn">{error}</div> : null}
      <Suspense fallback={<div className="price-chart chart-loading">Loading chart…</div>}>
        <PriceChart symbol={symbol} spot={spot} expiration={exp} expectedMove={chain?.expected_move ?? null} />
      </Suspense>
      <div className="exps" role="tablist" aria-label="Expiration">
        {exps.map((e) => (
          <button
            key={e.date}
            role="tab"
            aria-selected={e.date === exp}
            className={e.monthly ? "monthly" : ""}
            onClick={() => setExp(e.date)}
            title={e.monthly ? "Monthly" : "Weekly"}
          >
            {shortDate(e.date)}
            <span className="sub"> {e.dte}d</span>
          </button>
        ))}
      </div>

      <div className="trade-grid">
        <div className="chain-wrap" ref={scroller}>
          <table className="chain">
            <thead>
              <tr>
                <th colSpan={4} className="side">Calls</th>
                <th />
                <th colSpan={4} className="side">Puts</th>
              </tr>
              <tr>
                <th>OI</th>
                <th>Δ</th>
                <th>Bid</th>
                <th>Ask</th>
                <th className="k">Strike</th>
                <th>Bid</th>
                <th>Ask</th>
                <th>Δ</th>
                <th>OI</th>
              </tr>
            </thead>
            <tbody>
              {(chain?.rows ?? []).map((r) => {
                const callItm = spot !== null && r.strike < spot;
                const putItm = spot !== null && r.strike > spot;
                const held = [r.call?.symbol, r.put?.symbol].some((s) => s && inBuild.has(s));
                return (
                  <tr key={r.strike} className={`${r.strike === atmStrike ? "atm" : ""} ${held ? "inbuild" : ""}`}>
                    <td className={callItm ? "itm" : ""}>{r.call?.oi?.toLocaleString("en-US") ?? "–"}</td>
                    <td className={callItm ? "itm" : ""}>{num(r.call?.delta ?? null, 2)}</td>
                    {cell(r.call, r, "call", "bid")}
                    {cell(r.call, r, "call", "ask")}
                    <td className="k">
                      {strike(r.strike)}
                      {r.call && inBuild.has(r.call.symbol) ? <span className="tag">C{signed(inBuild.get(r.call.symbol)!)}</span> : null}
                      {r.put && inBuild.has(r.put.symbol) ? <span className="tag">P{signed(inBuild.get(r.put.symbol)!)}</span> : null}
                    </td>
                    {cell(r.put, r, "put", "bid")}
                    {cell(r.put, r, "put", "ask")}
                    <td className={putItm ? "itm" : ""}>{num(r.put?.delta ?? null, 2)}</td>
                    <td className={putItm ? "itm" : ""}>{r.put?.oi?.toLocaleString("en-US") ?? "–"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!chain && !error ? <div className="empty">Loading chain…</div> : null}
        </div>

        <div className="builder">
          <div className="quick">
            {BUILDERS.map((b) => (
              <button
                key={b.name}
                type="button"
                title={b.title}
                disabled={!chain || !exp || spot === null}
                onClick={() => {
                  if (!chain || !exp || spot === null) return;
                  const built = b.build(chain.rows, exp, params, spot, shares);
                  if (built) {
                    setLegs(built);
                    setCovered(b.name === "Covered call" ? Math.abs(built[0].quantity) * 100 : 0);
                    setAnaError(null);
                  } else if (b.name === "Covered call") {
                    setAnaError(
                      `A covered call needs 100 shares of ${symbol} per contract; this account holds ${shares}.`,
                    );
                  } else setAnaError(`Not enough strikes in this chain for a ${b.name.toLowerCase()}.`);
                }}
              >
                {b.name}
              </button>
            ))}
          </div>
          <div className="params">
            <label>
              Strangle Δ
              <input type="number" step="0.01" min="0.05" max="0.5" value={params.strangle}
                onChange={(e) => setParams({ ...params, strangle: Number(e.target.value) })} />
            </label>
            <label>
              Short Δ
              <input type="number" step="0.01" min="0.05" max="0.5" value={params.vertical}
                onChange={(e) => setParams({ ...params, vertical: Number(e.target.value) })} />
            </label>
            <label>
              Condor Δ
              <input type="number" step="0.01" min="0.05" max="0.5" value={params.condor}
                onChange={(e) => setParams({ ...params, condor: Number(e.target.value) })} />
            </label>
            <label>
              Width $
              <input type="number" step="1" min="0.5" value={params.width}
                onChange={(e) => setParams({ ...params, width: Number(e.target.value) })} />
            </label>
          </div>

          {legs.length ? (
            <table className="legs">
              <tbody>
                {legs.map((l, i) => (
                  <tr key={l.symbol}>
                    <td className="qty">
                      <button type="button" aria-label="Decrease" onClick={() => setLegs(legs.map((x, j) => (j === i ? { ...x, quantity: x.quantity - 1 } : x)).filter((x) => x.quantity !== 0))}>−</button>
                      <span className={tone(l.quantity)}>{signed(l.quantity)}</span>
                      <button type="button" aria-label="Increase" onClick={() => setLegs(legs.map((x, j) => (j === i ? { ...x, quantity: x.quantity + 1 } : x)).filter((x) => x.quantity !== 0))}>+</button>
                    </td>
                    <td className="l">
                      {shortDate(l.expiration)} {strike(l.strike)} {l.put_call === "P" ? "Put" : "Call"}
                    </td>
                    <td>{num(l.price, 2)}</td>
                    <td className="sub">Δ {num(l.delta, 2)}</td>
                    <td>
                      <button type="button" className="x" aria-label="Remove leg" onClick={() => setLegs(legs.filter((_, j) => j !== i))}>×</button>
                    </td>
                  </tr>
                ))}
                {coveredNow ? (
                  <tr>
                    <td className="qty">
                      <span className={tone(coveredNow)}>+{coveredNow}</span>
                    </td>
                    <td className="l" colSpan={4}>
                      shares held <span className="sub">(in the payoff, not in the order)</span>
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          ) : (
            <p className="hint">
              Pick a strategy above, or tap a bid to sell and an ask to buy; tap again to remove.
              Set quantities here. Legs are priced at mid.
            </p>
          )}
          {legs.length ? (
            <button type="button" className="link clear" onClick={() => setLegs([])}>
              Clear
            </button>
          ) : null}
          {anaError ? <div className="banner warn">{anaError}</div> : null}
          {legs.length ? (
            <button
              type="button"
              className="primary review"
              onClick={() =>
                openTicket({
                  title: `Open ${symbol} trade`,
                  account,
                  intent: "open",
                  underlying: symbol,
                  tradeId: null,
                  legs: legs.map((l) => ({ symbol: l.symbol, quantity: l.quantity, effect: "open" as const })),
                })
              }
            >
              Review order
            </button>
          ) : null}
          {analysis && spot !== null ? <Summary a={analysis} spot={spot} /> : null}

        </div>
      </div>
    </div>
  );
}

function Summary({ a, spot }: { a: Analysis; spot: number }) {
  const after = a.portfolio_after;
  const over = after.bp_used_pct !== null && after.bp_ceiling_pct !== null && after.bp_used_pct > after.bp_ceiling_pct;
  const rob = a.max_profit !== null && a.bp_estimate > 0 ? a.max_profit / a.bp_estimate : null;
  return (
    <>
      <div className="summary">
        <div>
          <span className="lbl">{a.credit ? "Credit" : "Debit"}</span>
          <span className="val">
            {a.price_per_unit !== null ? num(a.price_per_unit, 2) : money(Math.abs(a.net))}
            <span className="sub"> {money(Math.abs(a.net))} total</span>
          </span>
        </div>
        <div>
          <span className="lbl">Max profit</span>
          <span className="val pos">{a.unlimited_profit ? "Unlimited" : money(a.max_profit)}</span>
        </div>
        <div>
          <span className="lbl">Max loss</span>
          <span className="val neg">{a.unlimited_loss ? "Unlimited" : money(a.max_loss)}</span>
        </div>
        <div>
          <span className="lbl">Breakevens</span>
          <span className="val">{a.breakevens.length ? a.breakevens.map((b) => num(b, 2)).join(" / ") : "–"}</span>
        </div>
        <div>
          <span className="lbl" title="Probability of any profit at the first expiration, from at-the-money IV">POP</span>
          <span className="val">{pct(a.pop)}</span>
        </div>
        <div>
          <span className="lbl" title={a.bp_exact ? "Defined risk: the max loss" : "Reg T estimate; Schwab's preview in phase 3 is authoritative"}>
            BP effect{a.bp_exact ? "" : " ≈"}
          </span>
          <span className="val">
            {money(a.bp_estimate)}
            {rob !== null ? <span className="sub"> {pct(rob, 1)} on BP</span> : null}
          </span>
        </div>
        <div>
          <span className="lbl">Δ / Θ / Vega</span>
          <span className="val">
            <span className={tone(a.delta)}>{signed(a.delta, 0)}</span>
            {" / "}
            <span className={tone(a.theta)}>{signed(a.theta, 0)}</span>
            {" / "}
            {signed(a.vega, 0)}
          </span>
        </div>
        <div>
          <span className="lbl" title="Portfolio after this trade">Portfolio after</span>
          <span className="val">
            β-Δ {signed(after.delta_bw, 0)} · Θ {signed(after.theta, 0)} ·{" "}
            <span className={over ? "warn-text" : ""}>BP {pct(after.bp_used_pct)}</span>
            {after.bp_ceiling_pct !== null ? <span className="sub"> / {pct(after.bp_ceiling_pct)}</span> : null}
          </span>
        </div>
      </div>
      <PayoffChart a={a} spot={spot} />
    </>
  );
}
