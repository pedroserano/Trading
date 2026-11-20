import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  ALL,
  AccountContext,
  AccountsContext,
  TicketContext,
  ViewContext,
  type Account,
  type SchwabAccount,
} from "./context";
import { getJson } from "./api";
import Activity from "./Activity";
import MarketStrip from "./MarketStrip";
import {
  AutoBar,
  AutomationContext,
  AutomationsContext,
  PauseSwitch,
  type AutomationCtx,
  useAllAutomations,
  useAutomation,
} from "./Automation";
import Dashboard from "./Dashboard";
import Portfolio from "./Portfolio";
import PositionsTable from "./PositionsTable";
import Settings from "./Settings";
import OrderTicket, { type TicketSpec } from "./OrderTicket";
import { clock, money, pct, signed, tone } from "./format";
import type { Cover, SideCover, Snapshot } from "./types";
import { useSnapshot, type Link } from "./useSnapshot";
import Trade from "./Trade";
import Watchlist from "./Watchlist";

type Route =
  /** Positions; risk: the Portfolio figures (allocation, Greeks, theta ladder) instead. */
  | { page: "positions"; risk: boolean }
  | { page: "watchlist" }
  /** History: orders and trades (once Activity), or stats (once Dashboard). */
  | { page: "history"; tab: "orders" | "stats" }
  | { page: "settings"; section: string | null }
  | { page: "trade"; symbol: string };

function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/");
  // The watchlist used to be edited in Settings; old links land on the Watchlist screen.
  if (
    parts[0] === "watchlist" ||
    (parts[0] === "settings" && parts[1] === "watchlist")
  ) {
    return { page: "watchlist" };
  }
  // Portfolio, Activity and Dashboard were pages of their own; old links still land.
  if (parts[0] === "risk" || parts[0] === "portfolio")
    return { page: "positions", risk: true };
  if (parts[0] === "activity") return { page: "history", tab: "orders" };
  if (parts[0] === "dashboard") return { page: "history", tab: "stats" };
  if (parts[0] === "history")
    return { page: "history", tab: parts[1] === "stats" ? "stats" : "orders" };
  if (parts[0] === "settings")
    return { page: "settings", section: parts[1] || null };
  if (parts[0] === "trade")
    return {
      page: "trade",
      symbol: decodeURIComponent(parts[1] || "SPY").toUpperCase(),
    };
  return { page: "positions", risk: false };
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

function usePersisted<T extends string>(
  key: string,
  initial: T,
): [T, (v: T) => void] {
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

function useNow(ms: number) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now() / 1000), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

/** The Schwab accounts the app runs, by their Schwab names, and whether the simulated
 * account is hidden (Settings); read again every few minutes. */
function useSchwabAccounts(): {
  accounts: SchwabAccount[];
  hide_sim: boolean;
} | null {
  const [accounts, setAccounts] = useState<{
    accounts: SchwabAccount[];
    hide_sim: boolean;
  } | null>(null);
  useEffect(() => {
    let live = true;
    const read = async () => {
      try {
        const r = await getJson<{
          accounts: SchwabAccount[];
          hide_sim?: boolean;
        }>("/api/accounts/in-use");
        if (live) setAccounts({ accounts: r.accounts, hide_sim: !!r.hide_sim });
      } catch {
        /* kept as it was; the next read tries again */
      }
    };
    read();
    const t = window.setInterval(read, 300_000);
    return () => {
      live = false;
      window.clearInterval(t);
    };
  }, []);
  return accounts;
}

export default function App() {
  const [stored, setAccount] = usePersisted<Account>("account", "live");
  const read = useSchwabAccounts();
  const accounts = read?.accounts ?? null;
  const hideSim = read?.hide_sim ?? false;
  const first = accounts?.[0]?.key ?? "live";
  // All: every Schwab account together, once there is more than one.
  const all = stored === ALL && (accounts === null || accounts.length > 1);
  // A remembered account no longer in use (or the simulated one, hidden in Settings)
  // falls back to the first Schwab account. Under All, trading acts in the first one.
  const account =
    stored === ALL
      ? first
      : (stored === "sim" && !hideSim) ||
          accounts === null ||
          accounts.some((a) => a.key === stored)
        ? stored
        : first;
  const shown = all ? ALL : account;
  const { snapshot, link } = useSnapshot(shown);
  const route = useRoute();
  const [ticket, setTicket] = useState<TicketSpec | null>(null);
  // A line after an order is placed, at the top for a few seconds; a tap dismisses it.
  const [toast, setToast] = useState<string | null>(null);
  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 4000);
    return () => window.clearTimeout(t);
  }, [toast]);
  const automation = useAutomation(account);
  const automations = useAllAutomations(all ? (accounts ?? []).map((a) => a.key) : []);

  if (!snapshot) {
    return (
      <div className="loading">
        {link === "retrying"
          ? "Cannot reach the app server. Retrying…"
          : "Loading…"}
      </div>
    );
  }
  return (
    <TicketContext.Provider value={setTicket}>
      <AccountContext.Provider value={account}>
        <ViewContext.Provider value={shown}>
          <AccountsContext.Provider value={accounts ?? []}>
            <AutomationsContext.Provider value={automations}>
              <AutomationContext.Provider value={automation}>
                <Header
                  s={snapshot}
                  link={link}
                  route={route}
                  account={shown}
                  setAccount={setAccount}
                  accounts={accounts ?? []}
                  hideSim={hideSim}
                />
                <main>
                  <Banner s={snapshot} />
                  {route.page === "watchlist" ? <Watchlist /> : null}
                  {route.page === "history" ? (
                    <>
                      <div className="toolbar">
                        <div className="seg" role="group" aria-label="History">
                          <a
                            className="seg-link"
                            href="#/history"
                            aria-pressed={route.tab === "orders"}
                          >
                            Orders &amp; trades
                          </a>
                          <a
                            className="seg-link"
                            href="#/history/stats"
                            aria-pressed={route.tab === "stats"}
                          >
                            Stats
                          </a>
                        </div>
                      </div>
                      {route.tab === "orders" ? <Activity /> : <Dashboard />}
                    </>
                  ) : null}
                  {route.page === "settings" ? (
                    <Settings section={route.section} />
                  ) : null}
                  {route.page === "trade" ? (
                    <Trade
                      symbol={route.symbol}
                      cover={coverHeld(snapshot, route.symbol, all ? account : null)}
                    />
                  ) : null}
                  {route.page === "positions" ? (
                    <PositionsPage
                      s={snapshot}
                      risk={route.risk}
                      accounts={all ? (accounts ?? []) : null}
                      automations={automations}
                    />
                  ) : null}
                </main>
                {toast ? (
                  <div className="toast" role="status" onClick={() => setToast(null)}>
                    {toast}
                  </div>
                ) : null}
                {ticket ? (
                  <OrderTicket
                    spec={ticket}
                    onClose={(placed, note) => {
                      setTicket(null);
                      ticket.onClosed?.(placed);
                      if (note) setToast(note);
                    }}
                  />
                ) : null}
              </AutomationContext.Provider>
            </AutomationsContext.Provider>
          </AccountsContext.Provider>
        </ViewContext.Provider>
      </AccountContext.Provider>
    </TicketContext.Provider>
  );
}

/** Positions: the trades, grouped by underlying or by DTE, or Risk: the Portfolio figures. */
function PositionsPage(props: {
  s: Snapshot;
  risk: boolean;
  /** Under All: the Schwab accounts, each with its automanage line. */
  accounts: SchwabAccount[] | null;
  automations: Record<string, AutomationCtx>;
}) {
  const { s, risk, accounts, automations } = props;
  // "dte": tap the DTE heading to order underlyings by their soonest expiration.
  const [view, setView] = usePersisted<View>("positions.view", "underlying");
  const [flaggedOnly, setFlaggedOnly] = usePersisted<"0" | "1">("positions.flagged", "0");
  // Simple: the columns that say how a trade is doing; Detail: every number.
  const [cols, setCols] = usePersisted<"simple" | "detail">("positions.cols", "simple");
  return (
    <>
      <MarketStrip />
      <div className="toolbar">
        <div className="seg" role="group" aria-label="Group positions">
          <button aria-pressed={!risk} onClick={() => (window.location.hash = "#/")}>
            By underlying
          </button>
          <button
            aria-pressed={risk}
            onClick={() => (window.location.hash = "#/risk")}
            title="Buying power by underlying and sector, Greeks by position, the theta ladder"
          >
            Risk
          </button>
        </div>
        {risk ? null : (
          <>
            <label className="check">
              <input
                type="checkbox"
                checked={flaggedOnly === "1"}
                onChange={(e) => setFlaggedOnly(e.target.checked ? "1" : "0")}
              />
              Flagged only ({s.totals.flags})
            </label>
            <div className="seg cols-toggle" role="group" aria-label="Columns">
              <button
                aria-pressed={cols === "simple"}
                onClick={() => setCols("simple")}
                title="How each trade is doing"
              >
                Simple
              </button>
              <button
                aria-pressed={cols === "detail"}
                onClick={() => setCols("detail")}
                title="Every number: prices, Greeks, short deltas, max loss"
              >
                Detail
              </button>
            </div>
          </>
        )}
      </div>
      {risk ? (
        <Portfolio />
      ) : (
        <>
          {accounts ? (
            // Under All: each account's automanage line and proposals in turn.
            accounts.map((x) =>
              automations[x.key] ? (
                <AutomationContext.Provider key={x.key} value={automations[x.key]}>
                  <AutoBar />
                </AutomationContext.Provider>
              ) : null,
            )
          ) : (
            <AutoBar />
          )}
          <PositionsTable s={s} view={view} onSort={setView} flaggedOnly={flaggedOnly === "1"} detail={cols === "detail"} />
          <p className="footnote">
            Positions are grouped by their trade history, including trades placed in thinkorswim;
            credit includes every roll. Positions with no known history are grouped by their legs.
          </p>
        </>
      )}
    </>
  );
}

/** What a new covered call or put can be written against: shares (long for calls, short
 * for puts) not already under an option, and ZEBRAs of that type (100 shares a unit, for
 * options expiring no later than the ZEBRA). Mirrors the server's cover_calls. */
function coverHeld(s: Snapshot, symbol: string, only: string | null): Cover {
  const found = s.underlyings.find((x) => x.symbol === symbol);
  // Under All, only the account the ticket trades in covers.
  const u =
    found && only
      ? { ...found, strategies: found.strategies.filter((st) => !st.account || st.account === only) }
      : found;
  const side = (pc: "C" | "P"): SideCover => {
    const [stockName, zebraName, coveredName] =
      pc === "C"
        ? ["Stock", "Call ZEBRA", "Covered call"]
        : ["Short stock", "Put ZEBRA", "Covered put"];
    const cover: SideCover = { shares: 0, zebras: [] };
    if (!u) return cover;
    let shares = u.strategies
      .filter((st) => st.name === stockName)
      .flatMap((st) => st.legs)
      .reduce((a, l) => a + Math.abs(l.quantity), 0);
    for (const st of u.strategies) {
      if (
        st.name === zebraName &&
        st.numbers.shares_like &&
        st.numbers.expiration
      ) {
        cover.zebras.push({
          expiration: st.numbers.expiration,
          shares: Math.abs(st.numbers.shares_like),
        });
      }
    }
    cover.zebras.sort((a, b) => a.expiration.localeCompare(b.expiration));
    // Options already written against them use them up, soonest first, shares before ZEBRAs.
    const written = u.strategies
      .filter((st) => st.name === coveredName && st.numbers.covered_by)
      .sort((a, b) =>
        (a.numbers.expiration ?? "").localeCompare(b.numbers.expiration ?? ""),
      );
    for (const st of written) {
      let need = st.legs.reduce((a, l) => a + Math.abs(l.quantity), 0) * 100;
      const fromShares = Math.min(shares, need);
      shares -= fromShares;
      need -= fromShares;
      for (const z of cover.zebras) {
        if (need <= 0) break;
        if (st.numbers.expiration && z.expiration < st.numbers.expiration)
          continue;
        const take = Math.min(z.shares, need);
        z.shares -= take;
        need -= take;
      }
    }
    cover.shares = shares;
    return cover;
  };
  return { C: side("C"), P: side("P") };
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
    <span
      className="light"
      title={s.error ?? `Quotes ${clock(s.data.quotes_at)}`}
    >
      <span className={`dot ${cls}`} />
      {text}
    </span>
  );
}

function Stat(props: {
  label: string;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <div className="stat" title={props.title}>
      <div className="stat-label">{props.label}</div>
      <div className="stat-value">{props.children}</div>
    </div>
  );
}

const ICONS: Record<string, React.ReactNode> = {
  positions: (
    <>
      <rect x="3" y="7" width="18" height="13" rx="2" />
      <path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M3 12h18" />
    </>
  ),
  portfolio: (
    <>
      <path d="M12 3v9h9" />
      <path d="M20.5 15A9 9 0 1 1 9 3.5" />
    </>
  ),
  watchlist: (
    <path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z" />
  ),
  trade: <path d="M4 8h14l-3-3M20 16H6l3 3" />,
  history: (
    <>
      <path d="M6 3h12v18l-2-1.5-2 1.5-2-1.5-2 1.5-2-1.5L6 21z" />
      <path d="M9 8h6M9 12h6M9 16h4" />
    </>
  ),
  dashboard: <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />,
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1" />
    </>
  ),
};

/** The menu: a row of links in the header on wide screens, a bar of icons with labels fixed
 * to the bottom of the screen on phones (styles.css). */
function Nav({ route }: { route: Route }) {
  const tab = (page: Route["page"], href: string, label: string) => (
    <a
      href={href}
      className={route.page === page ? "active" : ""}
      aria-current={route.page === page ? "page" : undefined}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">
        {ICONS[page]}
      </svg>
      <span>{label}</span>
    </a>
  );
  return (
    <nav className="tabs">
      {tab("positions", "#/", "Positions")}
      {tab("watchlist", "#/watchlist", "Watchlist")}
      {tab(
        "trade",
        route.page === "trade" ? `#/trade/${route.symbol}` : "#/trade/SPY",
        "Trade",
      )}
      {tab("history", "#/history", "History")}
      {tab("settings", "#/settings", "Settings")}
    </nav>
  );
}

/** Whether the portfolio stats fit on the menu bar's line in one row. When they do not, the
 * header puts them on a line of their own below it. */
function useStackedStats() {
  const header = useRef<HTMLElement>(null);
  const stats = useRef<HTMLDivElement>(null);
  const [on, setOn] = useState(false);
  useLayoutEffect(() => {
    const h = header.current;
    const st = stats.current;
    if (!h || !st) return;
    const measure = () => {
      const gap = (el: Element) =>
        parseFloat(getComputedStyle(el).columnGap) || 0;
      const kids = Array.from(st.children) as HTMLElement[];
      // The stats' width on one line, whatever their layout is now.
      const statsWidth =
        kids.reduce((w, k) => w + k.offsetWidth, 0) +
        gap(st) * Math.max(0, kids.length - 1);
      const others = (Array.from(h.children) as HTMLElement[]).filter(
        (k) => k !== st,
      );
      const cs = getComputedStyle(h);
      const room =
        h.clientWidth -
        parseFloat(cs.paddingLeft) -
        parseFloat(cs.paddingRight);
      const need =
        others.reduce((w, k) => w + k.offsetWidth, 0) +
        statsWidth +
        gap(h) * others.length;
      setOn(need > room);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(h);
    for (const k of [...Array.from(h.children), ...Array.from(st.children)])
      ro.observe(k);
    return () => ro.disconnect();
  }, []);
  return { header, stats, on };
}

function Header(props: {
  s: Snapshot;
  link: Link;
  route: Route;
  account: Account;
  setAccount: (a: Account) => void;
  accounts: SchwabAccount[];
  hideSim: boolean;
}) {
  const { s, link, route } = props;
  const stacked = useStackedStats();
  const a = s.account;
  const bp = a?.bp_used_pct ?? null;
  const ceiling = a?.bp_ceiling_pct ?? null;
  const thetaPct = s.totals.theta_pct_nl;
  const db = s.bands?.delta;
  const tb = s.bands?.theta;
  const out = (b?: { state: string | null }) =>
    b?.state === "below" || b?.state === "above" ? "warn-text" : "";
  const thetaTone = tb
    ? out(tb)
    : thetaPct === null
      ? ""
      : thetaPct < 0.001 || thetaPct > 0.003
        ? "warn-text"
        : "";
  const deltaTitle = db
    ? `Beta-weighted delta in SPY shares; ${pct(db.value, 1)} of net liq. ` +
      `Target (${s.bands?.bias}): ${pct(db.min, 0)} to ${pct(db.max, 0)}` +
      (out(db) ? `, now ${db.state} it` : "")
    : "Beta-weighted delta in SPY shares";
  const thetaTitle = tb
    ? `Target band ${pct(tb.min, 2)}–${pct(tb.max, 2)} of net liq` +
      (out(tb) ? `, now ${tb.state} it` : "")
    : "Target band 0.1%–0.3% of net liq";
  const exp = s.connection.expires_in_days;
  return (
    <header
      className={`top ${stacked.on ? "stacked" : ""}`}
      ref={stacked.header}
    >
      <div className="brand">
        <strong>Trading</strong>
        <div className="seg acct-switch" role="group" aria-label="Account">
          {props.accounts.length > 1 ? (
            <button
              aria-pressed={props.account === ALL}
              onClick={() => props.setAccount(ALL)}
              title="Every Schwab account together (not the simulated one)"
            >
              All
            </button>
          ) : null}
          {(props.accounts.length
            ? props.accounts
            : [{ key: "live", name: "Schwab", last4: "" }]
          ).map((x) => (
            <button
              key={x.key}
              aria-pressed={props.account === x.key}
              onClick={() => props.setAccount(x.key)}
              title={x.last4 ? `Schwab account …${x.last4}` : undefined}
            >
              {x.name}
            </button>
          ))}
          {props.hideSim ? null : (
            <button
              aria-pressed={props.account === "sim"}
              onClick={() => props.setAccount("sim")}
            >
              Simulated
            </button>
          )}
        </div>
        {props.account === ALL ? null : <PauseSwitch />}
        <DataLight s={s} link={link} />
        {a && props.account !== ALL ? (
          <span className="acct">
            …{a.last4} {a.type ? a.type.toLowerCase() : ""}
          </span>
        ) : null}
        <Nav route={route} />
        <span className="right">
          {s.mode === "live" ? (
            <a
              href="#/settings"
              className={`signin ${exp !== null && exp < 2 ? "warn-text" : ""}`}
              title="Days until the Schwab sign-in expires"
            >
              Schwab {exp === null ? "–" : `${exp.toFixed(1)}d`}
            </a>
          ) : null}
        </span>
      </div>
      <div className="stats" ref={stacked.stats}>
        <Stat label="Net liq">{money(a?.net_liq)}</Stat>
        <Stat
          label="BP used"
          title={`Maintenance requirement as a share of net liq. Ceiling at this VIX: ${pct(ceiling)}${
            a?.margin_type === "pm"
              ? " (portfolio margin: half the Reg T ceiling)"
              : ""
          }`}
        >
          <span
            className={
              bp !== null && ceiling !== null && bp > ceiling ? "warn-text" : ""
            }
          >
            {pct(bp)}
          </span>
          <span className="sub"> / {pct(ceiling)}</span>
          <Meter value={bp} max={ceiling} />
        </Stat>
        {out(db) ? (
          <Stat label="β-Δ SPY" title={deltaTitle}>
            <span className="warn-text">{signed(s.totals.delta_bw, 0)}</span>
            {db && db.value !== null ? (
              <span className="sub warn-text"> {pct(db.value, 1)}</span>
            ) : null}
          </Stat>
        ) : null}
        {thetaTone ? (
          <Stat label="Θ / day" title={thetaTitle}>
            <span className="warn-text">{signed(s.totals.theta, 0)}</span>
            <span className="sub warn-text"> {pct(thetaPct, 2)}</span>
          </Stat>
        ) : null}
        {!out(db) || !thetaTone ? (
          <Stat
            label="Greeks"
            title={
              `${!out(db) ? `β-Δ ${signed(s.totals.delta_bw, 0)} SPY shares (${pct(db?.value ?? null, 1)}), in its band. ` : ""}` +
              `${!thetaTone ? `Θ ${signed(s.totals.theta, 0)} a day (${pct(thetaPct, 2)}), in its band. ` : ""}` +
              "Shown here when outside its band; every figure is under Positions, Risk."
            }
          >
            <a href="#/risk" className="in-band">
              {!out(db) && !thetaTone
                ? "In band"
                : !out(db)
                  ? "Δ in band"
                  : "Θ in band"}
            </a>
          </Stat>
        ) : null}
        <Stat label="P/L open">
          <span className={tone(s.totals.pl)}>{signed(s.totals.pl, 0)}</span>
        </Stat>
        <Stat label="P/L day" title="Today's P/L on open positions">
          <span className={tone(s.totals.day_pl ?? null)}>
            {signed(s.totals.day_pl ?? null, 0)}
          </span>
        </Stat>
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
  if (s.error) return <div className="banner warn">{s.error}</div>;
  return null;
}

// Positions ---------------------------------------------------------------------------
