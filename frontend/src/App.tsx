import { useContext, useEffect, useMemo, useState } from "react";
import { AccountContext, TicketContext, type Account } from "./context";
import Activity from "./Activity";
import ChainHistory from "./ChainHistory";
import Dashboard from "./Dashboard";
import Settings from "./Settings";
import OrderTicket, { actionsFor, closeSpec, rollSpec, type TicketSpec } from "./OrderTicket";
import { clock, money, num, pct, shortDate, signed, strike, tone } from "./format";
import type { Snapshot, StrategyView, UnderlyingView } from "./types";
import { useSnapshot, type Link } from "./useSnapshot";
import Trade from "./Trade";
import Watchlist from "./Watchlist";

type Route =
  | { page: "positions" }
  | { page: "watchlist" }
  | { page: "activity" }
  | { page: "dashboard" }
  | { page: "settings"; section: string | null }
  | { page: "trade"; symbol: string };



function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/");
  if (parts[0] === "watchlist") return { page: "watchlist" };
  if (parts[0] === "activity") return { page: "activity" };
  if (parts[0] === "dashboard") return { page: "dashboard" };
  if (parts[0] === "settings") return { page: "settings", section: parts[1] || null };
  if (parts[0] === "trade") return { page: "trade", symbol: decodeURIComponent(parts[1] || "SPY").toUpperCase() };
  return { page: "positions" };
}

function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const on = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

type View = "underlying" | "dte";

function usePersisted<T extends string>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try {
      return (localStorage.getItem(key) as T) || initial;
    } catch {
      return initial;
    }
  });
  const set = (next: T) => {
    setV(next);
    try {
      localStorage.setItem(key, next);
    } catch {
      /* storage unavailable */
    }
  };
  return [v, set];
}

function useMediaQuery(q: string): boolean {
  const [match, setMatch] = useState(() => window.matchMedia(q).matches);
  useEffect(() => {
    const m = window.matchMedia(q);
    const on = () => setMatch(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, [q]);
  return match;
}

function useNow(ms: number) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now() / 1000), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

export default function App() {
  const [account, setAccount] = usePersisted<Account>("account", "live");
  const { snapshot, link } = useSnapshot(account);
  const route = useRoute();
  const [ticket, setTicket] = useState<TicketSpec | null>(null);
  const [view, setView] = usePersisted<View>("positions.view", "underlying");
  const [flaggedOnly, setFlaggedOnly] = usePersisted<"0" | "1">("positions.flagged", "0");

  if (!snapshot) {
    return (
      <div className="loading">
        {link === "retrying" ? "Cannot reach the app server. Retrying…" : "Loading…"}
      </div>
    );
  }
  return (
    <TicketContext.Provider value={setTicket}>
      <AccountContext.Provider value={account}>
      <Header s={snapshot} link={link} route={route} account={account} setAccount={setAccount} />
      <main>
        <Banner s={snapshot} />
        {route.page === "watchlist" ? <Watchlist /> : null}
        {route.page === "activity" ? <Activity /> : null}
        {route.page === "dashboard" ? <Dashboard /> : null}
        {route.page === "settings" ? <Settings section={route.section} /> : null}
        {route.page === "trade" ? <Trade symbol={route.symbol} /> : null}
        {route.page === "positions" ? (
          <>
        <div className="toolbar">
          <div className="seg" role="group" aria-label="Group positions">
            <button aria-pressed={view === "underlying"} onClick={() => setView("underlying")}>
              By underlying
            </button>
            <button aria-pressed={view === "dte"} onClick={() => setView("dte")}>
              By DTE
            </button>
          </div>
          <label className="check">
            <input
              type="checkbox"
              checked={flaggedOnly === "1"}
              onChange={(e) => setFlaggedOnly(e.target.checked ? "1" : "0")}
            />
            Flagged only ({snapshot.totals.flags})
          </label>
        </div>
        <Positions s={snapshot} view={view} flaggedOnly={flaggedOnly === "1"} />
        <p className="footnote">
          Positions are grouped by their trade history, including trades placed in thinkorswim;
          credit includes every roll. Positions with no known history are grouped by their legs.
        </p>
          </>
        ) : null}
      </main>
      {ticket ? <OrderTicket spec={ticket} onClose={() => setTicket(null)} /> : null}
      </AccountContext.Provider>
    </TicketContext.Provider>
  );
}

function DataLight({ s, link }: { s: Snapshot; link: Link }) {
  const now = useNow(2000);
  const age = s.data.quotes_at ? now - s.data.quotes_at : Infinity;
  let cls = "ok";
  let text = s.data.source === "stream" ? "Streaming" : "Live";
  if (link !== "open") {
    cls = "bad";
    text = "No link to app";
  } else if (s.needs || !s.data.quotes_at) {
    cls = "bad";
    text = "No data";
  } else if (s.session === "closed") {
    cls = "dim";
    text = `Closed · ${clock(s.data.quotes_at)}`;
  } else if (age > 30 || s.error) {
    cls = "warn";
    text = `Stale · ${clock(s.data.quotes_at)}`;
  }
  return (
    <span className="light" title={s.error ?? `Quotes ${clock(s.data.quotes_at)}`}>
      <span className={`dot ${cls}`} />
      {text}
    </span>
  );
}

function Stat(props: { label: string; children: React.ReactNode; title?: string }) {
  return (
    <div className="stat" title={props.title}>
      <div className="stat-label">{props.label}</div>
      <div className="stat-value">{props.children}</div>
    </div>
  );
}

function Nav({ route }: { route: Route }) {
  const tab = (page: Route["page"], href: string, label: string) => (
    <a href={href} className={route.page === page ? "active" : ""} aria-current={route.page === page ? "page" : undefined}>
      {label}
    </a>
  );
  return (
    <nav className="tabs">
      {tab("positions", "#/", "Positions")}
      {tab("watchlist", "#/watchlist", "Watchlist")}
      {tab("trade", route.page === "trade" ? `#/trade/${route.symbol}` : "#/trade/SPY", "Trade")}
      {tab("activity", "#/activity", "Activity")}
      {tab("dashboard", "#/dashboard", "Dashboard")}
      {tab("settings", "#/settings", "Settings")}
    </nav>
  );
}

function Header(props: {
  s: Snapshot;
  link: Link;
  route: Route;
  account: Account;
  setAccount: (a: Account) => void;
}) {
  const { s, link, route } = props;
  const a = s.account;
  const bp = a?.bp_used_pct ?? null;
  const ceiling = a?.bp_ceiling_pct ?? null;
  const thetaPct = s.totals.theta_pct_nl;
  const thetaTone =
    thetaPct === null ? "" : thetaPct < 0.001 || thetaPct > 0.003 ? "warn-text" : "";
  const spyIvr = s.ivr.SPY;
  const exp = s.connection.expires_in_days;
  return (
    <header className="top">
      <div className="brand">
        <strong>Trading</strong>
        {s.mode === "demo" ? <span className="badge demo">DEMO</span> : null}
        <div className="seg acct-switch" role="group" aria-label="Account">
          <button aria-pressed={props.account === "live"} onClick={() => props.setAccount("live")}>
            Schwab
          </button>
          <button aria-pressed={props.account === "sim"} onClick={() => props.setAccount("sim")}>
            Simulated
          </button>
        </div>
        {a ? (
          <span className="acct">
            …{a.last4} {a.type ? a.type.toLowerCase() : ""}
          </span>
        ) : null}
        <Nav route={route} />
      </div>
      <div className="stats">
        <Stat label="Net liq">{money(a?.net_liq)}</Stat>
        <Stat
          label="BP used"
          title={`Maintenance requirement as a share of net liq. Ceiling at this VIX: ${pct(ceiling)}`}
        >
          <span className={bp !== null && ceiling !== null && bp > ceiling ? "warn-text" : ""}>
            {pct(bp)}
          </span>
          <span className="sub"> / {pct(ceiling)}</span>
          <Meter value={bp} max={ceiling} />
        </Stat>
        <Stat label="β-Δ SPY" title="Beta-weighted delta in SPY shares">
          <span className={tone(s.totals.delta_bw)}>{signed(s.totals.delta_bw, 0)}</span>
        </Stat>
        <Stat label="Θ / day" title="Target band 0.1%–0.3% of net liq">
          <span className={tone(s.totals.theta)}>{signed(s.totals.theta, 0)}</span>
          <span className={`sub ${thetaTone}`}> {pct(thetaPct, 2)}</span>
        </Stat>
        <Stat label="P/L open">
          <span className={tone(s.totals.pl)}>{signed(s.totals.pl, 0)}</span>
        </Stat>
        <Stat label="VIX">{num(s.market.vix, 2)}</Stat>
        <Stat
          label="SPY IVR"
          title={spyIvr ? `From VIX over ${spyIvr.days} days. IV percentile ${spyIvr.percentile}` : ""}
        >
          {spyIvr && spyIvr.rank !== null ? Math.round(spyIvr.rank) : "–"}
        </Stat>
      </div>
      <div className="right">
        <DataLight s={s} link={link} />
        {s.mode === "live" ? (
          <a
            href="#/settings"
            className={`signin ${exp !== null && exp < 2 ? "warn-text" : ""}`}
            title="Days until the Schwab sign-in expires"
          >
            Schwab {exp === null ? "–" : `${exp.toFixed(1)}d`}
          </a>
        ) : null}
      </div>
    </header>
  );
}

function Meter({ value, max }: { value: number | null; max: number | null }) {
  if (value === null || max === null) return null;
  const w = Math.min(100, (value / max) * 100);
  return (
    <span className="meter" aria-hidden>
      <span style={{ width: `${w}%` }} className={value > max ? "over" : ""} />
    </span>
  );
}

function Banner({ s }: { s: Snapshot }) {
  if (s.needs) {
    return (
      <div className="banner bad">
        {s.needs.message}{" "}
        <a href="#/settings">
          {s.needs.what === "connect" ? "Connect to Schwab" : "Choose account"}
        </a>
      </div>
    );
  }
  if (s.connection.visible_accounts && s.connection.visible_accounts > 1) {
    return (
      <div className="banner warn">
        The Schwab sign-in can see {s.connection.visible_accounts} accounts. Live trading will
        require it to see only the trading account. <a href="#/settings">Details</a>
      </div>
    );
  }
  if (s.error) return <div className="banner warn">{s.error}</div>;
  return null;
}

// Positions ---------------------------------------------------------------------------

interface Row {
  under: UnderlyingView;
  st: StrategyView;
}

function Positions({ s, view, flaggedOnly }: { s: Snapshot; view: View; flaggedOnly: boolean }) {
  const compact = useMediaQuery("(max-width: 700px)");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const toggle = (k: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });

  const filter = (st: StrategyView) => !flaggedOnly || st.flags.length > 0;
  const flat: Row[] = useMemo(
    () =>
      s.underlyings
        .flatMap((u) => u.strategies.map((st) => ({ under: u, st })))
        .filter((r) => filter(r.st))
        .sort((a, b) => (a.st.numbers.dte ?? 9999) - (b.st.numbers.dte ?? 9999)),
    [s, flaggedOnly],
  );

  if (s.underlyings.length === 0) {
    return <div className="empty">{s.needs ? "" : "No open positions."}</div>;
  }
  const showUnder = view === "dte";
  return (
    <div className="table-wrap">
      <table className={`positions ${compact ? "compact" : ""}`}>
        <thead>
          {compact ? (
            <tr>
              <th className="l">Strategy</th>
              <th>DTE</th>
              <th>P/L</th>
              <th>% max</th>
            </tr>
          ) : (
            <tr>
              <th className="l">{showUnder ? "Underlying / strategy" : "Strategy"}</th>
              <th>Exp</th>
              <th>DTE</th>
              <th>Qty</th>
              <th title="Opening price per unit; credit or debit">Open</th>
              <th title="Current price per unit">Mark</th>
              <th>P/L</th>
              <th title="P/L as a share of max profit">% max</th>
              <th title="Delta in shares">Δ</th>
              <th title="Theta, dollars per day">Θ</th>
              <th title="Delta of each short option">Short Δ</th>
              <th>Max loss</th>
              <th className="l">Flags</th>
            </tr>
          )}
        </thead>
        <tbody>
          {view === "underlying"
            ? s.underlyings.map((u) => {
                const sts = u.strategies.filter(filter);
                if (!sts.length) return null;
                return (
                  <UnderGroup
                    key={u.symbol}
                    u={u}
                    sts={sts}
                    open={open}
                    toggle={toggle}
                    compact={compact}
                  />
                );
              })
            : flat.map((r, i) => (
                <StrategyRows
                  key={`${r.under.symbol}-${i}`}
                  id={`${r.under.symbol}-${r.st.name}-${r.st.numbers.expiration}-${i}`}
                  st={r.st}
                  under={r.under.symbol}
                  open={open}
                  toggle={toggle}
                  compact={compact}
                />
              ))}
        </tbody>
      </table>
    </div>
  );
}

function UnderGroup(props: {
  u: UnderlyingView;
  sts: StrategyView[];
  open: Set<string>;
  toggle: (k: string) => void;
  compact: boolean;
}) {
  const { u, compact } = props;
  return (
    <>
      <tr className="under">
        <td className="l" colSpan={compact ? 2 : 6}>
          <a className="sym" href={`#/trade/${encodeURIComponent(u.symbol)}`} onClick={(e) => e.stopPropagation()}>
            {u.symbol}
          </a>
          <span className="px">{num(u.price, 2)}</span>
          <span className={`chg ${tone(u.change_pct)}`}>
            {u.change_pct === null ? "" : `${signed(u.change_pct, 2)}%`}
          </span>
          <span
            className="ivr"
            title={
              u.ivr
                ? `IV rank over ${u.ivr.days} days${u.ivr.source === "VIX" ? " of VIX" : " of recorded IV"}; percentile ${u.ivr.percentile}`
                : "No IV history yet. Recording starts with the first daily snapshot."
            }
          >
            IVR {u.ivr && u.ivr.rank !== null ? Math.round(u.ivr.rank) : "–"}
            {u.ivr && u.ivr.source === "recorded" && u.ivr.days < 252 ? (
              <span className="sub"> ({u.ivr.days}d)</span>
            ) : null}
          </span>
          <span className="beta">β {u.beta === null ? "–" : u.beta.toFixed(2)}</span>
        </td>
        <td className={tone(u.pl)}>{signed(u.pl)}</td>
        <td />
        {compact ? null : (
          <>
            <td className={tone(u.delta)} title={`β-weighted: ${signed(u.delta_bw, 0)}`}>
              {signed(u.delta, 0)}
            </td>
            <td className={tone(u.theta)}>{signed(u.theta, 0)}</td>
            <td colSpan={3} />
          </>
        )}
      </tr>
      {props.sts.map((st, i) => (
        <StrategyRows
          key={i}
          id={`${u.symbol}-${st.name}-${st.numbers.expiration}-${i}`}
          st={st}
          open={props.open}
          toggle={props.toggle}
          compact={compact}
          underSymbol={u.symbol}
        />
      ))}
    </>
  );
}

function Flags({ st }: { st: StrategyView }) {
  return (
    <span className="flags">
      {st.flags.map((f) => (
        <span key={f.kind} className={`flag ${f.kind}`} title={f.detail}>
          {f.label}
        </span>
      ))}
    </span>
  );
}

function perUnit(st: StrategyView, total: number | null): number | null {
  const n = st.numbers;
  if (total === null || n.contracts === 0 || st.legs.some((l) => l.kind === "EQUITY")) return null;
  return Math.abs(total) / (n.contracts * 100);
}

function StrategyRows(props: {
  id: string;
  st: StrategyView;
  under?: string;
  open: Set<string>;
  toggle: (k: string) => void;
  compact: boolean;
  underSymbol?: string;
}) {
  const { st, id, compact } = props;
  const n = st.numbers;
  const expanded = props.open.has(id);
  const openPx = perUnit(st, n.open_value);
  const markPx = perUnit(st, n.value);
  return (
    <>
      <tr
        className={`strat ${expanded ? "expanded" : ""}`}
        onClick={() => props.toggle(id)}
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            props.toggle(id);
          }
        }}
        aria-expanded={expanded}
      >
        <td className="l name">
          <span className="caret">{expanded ? "▾" : "▸"}</span>
          {props.under ? <span className="sym-inline">{props.under}</span> : null}
          {st.name}
          {compact ? (
            <span className="sub">
              {" "}
              {n.contracts ? `×${n.contracts} ` : ""}
              {shortDate(n.expiration)}
            </span>
          ) : null}
          {!st.defined_risk && st.legs.some((l) => l.quantity < 0) ? (
            <span className="undef" title="Undefined risk">
              U
            </span>
          ) : null}
          {compact && st.flags.length ? <Flags st={st} /> : null}
        </td>
        {compact ? (
          <>
            <td>{n.dte ?? "–"}</td>
            <td className={tone(n.pl)}>{signed(n.pl)}</td>
            <td>{pct(n.pct_max_profit)}</td>
          </>
        ) : (
          <>
            <td>{shortDate(n.expiration)}</td>
            <td>{n.dte ?? "–"}</td>
            <td>{n.contracts || "–"}</td>
            <td>
              {openPx === null ? money(Math.abs(n.open_value)) : num(openPx, 2)}
              <span className="sub"> {n.credit ? "cr" : "db"}</span>
            </td>
            <td>{markPx === null ? money(n.value === null ? null : Math.abs(n.value)) : num(markPx, 2)}</td>
            <td className={tone(n.pl)}>{signed(n.pl)}</td>
            <td>{pct(n.pct_max_profit)}</td>
            <td className={tone(n.delta)}>{signed(n.delta, 0)}</td>
            <td className={tone(n.theta)}>{signed(n.theta, 0)}</td>
            <td>
              {n.short_deltas
                .map((d) => (d === null ? "–" : Math.abs(d).toFixed(2)))
                .join(" / ") || "–"}
            </td>
            <td>{n.max_loss === null ? (st.defined_risk ? "–" : "∞") : money(n.max_loss)}</td>
          </>
        )}
        {compact ? null : (
          <td className="l">
            <Flags st={st} />
          </td>
        )}
      </tr>
      {expanded
        ? st.legs.map((l) => (
            <tr className="leg" key={l.symbol}>
              <td className="l">
                <span className={tone(l.quantity)}>{signed(l.quantity)}</span>{" "}
                {l.kind === "OPTION"
                  ? `${shortDate(l.expiration)} ${strike(l.strike)} ${l.put_call === "P" ? "Put" : "Call"}`
                  : "Shares"}
              </td>
              {compact ? (
                <>
                  <td>{l.dte ?? ""}</td>
                  <td>{num(l.mark, 2)}</td>
                  <td title="Per-share delta">{num(l.delta, 2)}</td>
                </>
              ) : (
                <>
                  <td />
                  <td>{l.dte ?? ""}</td>
                  <td />
                  <td>{num(l.avg_price, 2)}</td>
                  <td>{num(l.mark, 2)}</td>
                  <td colSpan={2} className="sub">
                    {l.iv === null ? "" : `IV ${l.iv.toFixed(1)}`}
                  </td>
                  <td title="Per-share delta">{num(l.delta, 2)}</td>
                  <td title="Per-share theta">{num(l.theta, 3)}</td>
                  <td colSpan={3} />
                </>
              )}
            </tr>
          ))
        : null}
      {expanded && st.legs.some((l) => l.kind === "OPTION") ? <ActionsRow st={st} under={props.under ?? props.underSymbol} cols={compact ? 4 : 13} /> : null}
    </>
  );
}

function ActionsRow({ st, under, cols }: { st: StrategyView; under: string | undefined; cols: number }) {
  const open = useContext(TicketContext);
  const account = useContext(AccountContext);
  const [history, setHistory] = useState(false);
  if (!under) return null;
  const acts = actionsFor(st);
  return (
    <tr className="leg actions">
      <td className="l" colSpan={cols}>
        {acts.close.map((side) => (
          <button key={`c${side}`} type="button" className="small" onClick={() => open(closeSpec(st, under, account, side))}>
            Close{side === "P" ? " puts" : side === "C" ? " calls" : ""}
          </button>
        ))}
        {acts.roll.map((side) => (
          <button key={`r${side}`} type="button" className="small" onClick={() => open(rollSpec(st, under, account, side))}>
            Roll{side === "P" ? " puts" : side === "C" ? " calls" : ""}
          </button>
        ))}
        {acts.roll.length === 0 ? (
          <span className="sub" title="Schwab accepts at most four legs in one order">
            No roll: it would need more than four legs.{" "}
          </span>
        ) : null}
        {st.trade_id !== null ? (
          <button type="button" className="small" onClick={() => setHistory(true)}>
            History{st.rolls ? ` · ${st.rolls} roll${st.rolls > 1 ? "s" : ""}` : ""}
          </button>
        ) : null}
        {history && st.trade_id !== null ? <ChainHistory account={account} chainId={st.trade_id} onClose={() => setHistory(false)} /> : null}
        {account === "live" ? <span className="sub"> Preview against Schwab; live orders arrive in phase 4.</span> : null}
      </td>
    </tr>
  );
}
