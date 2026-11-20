import { Fragment, useCallback, useMemo, useRef, useState } from "react";
import Stars, { liquidityTitle } from "./Stars";
import { getJson, postForm, postJson } from "./api";
import { usePolling } from "./usePolling";
import EarningsFlag from "./Earnings";
import Scanner from "./Scanner";
import { num, pct, signed, tone } from "./format";
import type { EarningsStatus, PriceExtreme, WatchRow } from "./types";
import { ivrTitle } from "./format";

type Key = "symbol" | "ivr" | "iv30" | "change" | "em" | "range" | "move" | "liq" | "earnings" | "float";

const sorters: Record<Key, (r: WatchRow) => number | string> = {
  symbol: (r) => r.symbol,
  ivr: (r) => r.ivr?.rank ?? -1,
  liq: (r) => r.liquidity ?? -1,
  iv30: (r) => r.iv30 ?? -1,
  change: (r) => r.change_pct ?? -999,
  em: (r) => (r.expected_move && r.price ? r.expected_move / r.price : -1),
  range: (r) => r.extreme?.range_pos ?? -1,
  move: (r) => r.extreme?.move_z ?? -999,
  // The report's day, then before the open, unknown time, after the close. No date: "" (kept
  // last in either direction by the sort).
  earnings: (r) => (r.earnings?.date ? `${r.earnings.date} ${TIME_ORDER[r.earnings.time ?? ""] ?? 1}` : ""),
  // No float (funds, indexes): "" too.
  float: (r) => r.float?.shares ?? "",
};

/** Ascending first, like Symbol: the soonest report on top. */
const ASCENDING_FIRST: Key[] = ["symbol", "earnings", "float"];

/** 14.5B, 460M, 8.2M. */
function shares(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e8 ? 0 : 1)}M`;
  return `${Math.round(n / 1e3)}K`;
}

const TIME_ORDER: Record<string, number> = { bmo: 0, "": 1, amc: 2 };

/** "Mon, Nov 23 · Before the open": the heading over one day's reports, one side of the bell. */
function earningsGroup(r: WatchRow): string | null {
  const e = r.earnings;
  if (!e?.date) return null;
  const d = new Date(`${e.date}T12:00:00`);
  const day = d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  const when = e.time === "bmo" ? "Before the open" : e.time === "amc" ? "After the close" : "Time not announced";
  return `${day} · ${when}`;
}

function shortDay(iso: string): string {
  return new Date(`${iso}T12:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/** Your date for one stock: a date, before the open or after the close, Save. */
function EarningsEditor({
  row,
  onDone,
}: {
  row: WatchRow;
  onDone: (changed: boolean) => void;
}) {
  const e = row.earnings;
  const [day, setDay] = useState(e?.date ?? "");
  const [time, setTime] = useState<string>(e?.time ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const save = async (date: string | null) => {
    setBusy(true);
    try {
      await postJson("/api/earnings/set", { symbol: row.symbol, date, time: time || null });
      onDone(true);
    } catch (x) {
      setErr(String((x as Error).message));
      setBusy(false);
    }
  };
  return (
    <form
      className="earn-edit"
      onSubmit={(ev) => {
        ev.preventDefault();
        if (day) save(day);
      }}
    >
      <b>{row.symbol} earnings</b>
      <input type="date" value={day} onChange={(ev) => setDay(ev.target.value)} aria-label="Report date" required />
      <select value={time} onChange={(ev) => setTime(ev.target.value)} aria-label="Before or after the bell">
        <option value="bmo">Before the open</option>
        <option value="amc">After the close</option>
        <option value="">Time not given</option>
      </select>
      <button className="primary" type="submit" disabled={busy || !day}>
        Save
      </button>
      {row.earnings_mine ? (
        <button type="button" disabled={busy} onClick={() => save(null)} title="Remove your date; the source's date, if any, applies again">
          Remove mine
        </button>
      ) : null}
      <button type="button" className="link" onClick={() => onDone(false)}>
        Cancel
      </button>
      {err ? <span className="warn-text">{err}</span> : null}
    </form>
  );
}

/** Where the price sits in its 52-week range: a bar with a marker, amber near either end. */
function RangeBar({ x }: { x: PriceExtreme | null | undefined }) {
  if (!x || x.range_pos === null) return <span className="dim-text">–</span>;
  const pos = Math.max(0, Math.min(1, x.range_pos));
  const near = x.reasons.some((r) => r.includes("52-week"));
  return (
    <span className={`range ${near ? "near" : ""}`}>
      <span className="range-track">
        <span className="range-dot" style={{ left: `${pos * 100}%` }} />
      </span>
      <span className="range-pct">{Math.round(pos * 100)}%</span>
    </span>
  );
}

export default function Watchlist() {
  const [rows, setRows] = useState<WatchRow[] | null>(null);
  // Your view per underlying, for the scanner: absent follows the portfolio bias.
  const [error, setError] = useState<string | null>(null);
  const [sort, setSort] = useState<{ key: Key; desc: boolean }>({ key: "symbol", desc: false });
  const [earnStatus, setEarnStatus] = useState<EarningsStatus | null>(null);
  // The stock whose earnings date is being set, its editor under its row.
  const [editingEarn, setEditingEarn] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (current: () => boolean = () => true) => {
    try {
      const r = await getJson<{ rows: WatchRow[]; earnings?: EarningsStatus }>(
        "/api/watchlist",
      );
      if (!current()) return;
      setRows(r.rows);
      setEarnStatus(r.earnings ?? null);
      setError(null);
    } catch (e) {
      setError(String((e as Error).message));
    }
  }, []);

  usePolling(load, 10000);

  // Editing: a ticker box and Import on top, a × on each row.
  const [ticker, setTicker] = useState("");
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState<Set<string>>(new Set());
  const [note, setNote] = useState<{ text: string; warn: boolean } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const edit = async (call: () => Promise<{ message: string }>) => {
    setBusy(true);
    try {
      const out = await call();
      setNote(out.message ? { text: out.message, warn: /Skipped|Not recognised|No symbols/.test(out.message) } : null);
      await load();
    } catch (e) {
      setNote({ text: String((e as Error).message), warn: true });
    } finally {
      setBusy(false);
    }
  };
  const add = () => {
    if (!ticker.trim()) return;
    edit(async () => {
      const out = await postJson<{ message: string }>("/api/watchlist/add", { text: ticker, mode: "add" });
      setTicker("");
      return out;
    });
  };
  const importFile = (f: File) => {
    const fd = new FormData();
    fd.append("file", f);
    fd.append("mode", "add");
    edit(() => postForm<{ message: string }>("/api/watchlist/upload", fd));
  };
  const remove = async (symbol: string) => {
    setRemoving((s) => new Set(s).add(symbol));
    await edit(() => postJson<{ message: string }>("/api/watchlist/remove", { symbol }));
    setRemoving((s) => {
      const n = new Set(s);
      n.delete(symbol);
      return n;
    });
  };

  const editor = (
    <div className="watch-edit">
      <form
        className="watch-add"
        onSubmit={(e) => {
          e.preventDefault();
          add();
        }}
      >
        <input
          type="text"
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="Add ticker"
          aria-label="Ticker to add"
          autoCapitalize="characters"
          autoCorrect="off"
          spellCheck={false}
        />
        <button className="primary" type="submit" disabled={busy || !ticker.trim()}>
          Add
        </button>
        <button
          type="button"
          disabled={busy}
          title="Add the symbols from a thinkorswim watchlist export (the watchlist's menu, Export, To file)"
          onClick={() => fileRef.current?.click()}
        >
          Import
        </button>
        <input
          ref={fileRef}
          type="file"
          hidden
          accept=".csv,.txt,text/csv,text/plain"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) importFile(f);
            e.target.value = "";
          }}
        />
      </form>
      {note ? <div className={`watch-note ${note.warn ? "warn-text" : "ok-text"}`}>{note.text}</div> : null}
    </div>
  );

  const sorted = useMemo(() => {
    if (!rows) return [];
    const f = sorters[sort.key];
    return [...rows].sort((a, b) => {
      const x = f(a);
      const y = f(b);
      // No earnings date or no float: always at the bottom.
      if ((sort.key === "earnings" || sort.key === "float") && (x === "" || y === ""))
        return x === y ? 0 : x === "" ? 1 : -1;
      const c = x < y ? -1 : x > y ? 1 : 0;
      return sort.desc ? -c : c;
    });
  }, [rows, sort]);

  const th = (key: Key, label: string, title?: string) => (
    <th
      title={title}
      className={`sortable ${key === "symbol" || key === "earnings" ? "l" : ""}`}
      aria-sort={sort.key === key ? (sort.desc ? "descending" : "ascending") : "none"}
      onClick={() => setSort({ key, desc: sort.key === key ? !sort.desc : !ASCENDING_FIRST.includes(key) })}
    >
      {label}
      {sort.key === key ? (sort.desc ? " ↓" : " ↑") : ""}
    </th>
  );

  if (error && !rows) return <div className="banner warn">{error}</div>;
  if (!rows) return <div className="empty">Loading…</div>;
  if (!rows.length) {
    return (
      <>
        {editor}
        <div className="empty">Add a ticker to start.</div>
      </>
    );
  }
  return (
    <>
      <Scanner />
      {editor}
      <div className="toolbar">
        <span className="dim-text">
          IV and liquidity refresh every 15 minutes. Click a row to open it on the Trade screen.
        </span>
      </div>
      {earnStatus ? (
        <div className="earn-status dim-text">
          <span
            title={
              "Where earnings dates come from: Nasdaq's calendar (dates announced, about five weeks ahead, and each stock's last report)" +
              ". Refreshed every six hours. Tap a stock's Earnings cell to set a date yourself; yours win."
            }
          >
            Earnings dates: {earnStatus.provider} · {earnStatus.known} of {earnStatus.stocks} stocks
            {earnStatus.refreshed_at
              ? ` · ${new Date(earnStatus.refreshed_at * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}`
              : ""}
          </span>{" "}
          <button
            className="link"
            disabled={refreshing}
            onClick={async () => {
              setRefreshing(true);
              try {
                const out = await postJson<{ earnings: EarningsStatus }>("/api/earnings/refresh", {});
                setEarnStatus(out.earnings);
                await load();
              } catch (x) {
                setNote({ text: String((x as Error).message), warn: true });
              } finally {
                setRefreshing(false);
              }
            }}
          >
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
          {earnStatus.error ? <span className="warn-text"> {earnStatus.error}</span> : null}
        </div>
      ) : null}
      <div className="table-wrap">
        <table className="positions watch">
          <thead>
            <tr>
              {th("symbol", "Symbol")}
              <th>Last</th>
              {th("change", "Chg")}
              {th("range", "52w", "Where the price sits in its 52-week range of closes, 0% at the low")}
              {th("move", "Move", "The last few sessions' move against the move implied volatility expected, in standard deviations")}
              {th("iv30", "IV30", "30-day at-the-money implied volatility")}
              {th("ivr", "IVR", "IV rank over a year: from DoltHub (D), or for ETFs it lacks a Cboe volatility index (C), until this app has recorded a year of its own")}
              <th title="IV percentile">IVP</th>
              <th title="Put skew: how much more implied volatility the 25-delta put carries than the 25-delta call, in volatility points. High after a drop means puts are especially expensive, part of the scanner's drop signal.">
                Skew
              </th>
              {th("liq", "Liquidity", "1 to 5 stars from the at-the-money bid/ask and open interest, after tastylive")}
              {th("em", "Exp. move", "One standard deviation move to the expiration shown")}
              <th>Exp</th>
              <th title="At-the-money bid-ask spread as a share of the mark">Spread</th>
              <th title="Open interest at the money, call plus put">ATM OI</th>
              {th("float", "Float", "Shares free to trade, from Schwab, with how much of the float trades on an average day (10 days). Under 20 million shares is a low float, highlighted: it can move sharply. Funds and indexes have none. Schwab gives no short interest")}
              {th("earnings", "Earnings", "Next earnings report: date, and before the open or after the close. Sorts soonest first, by day and side of the bell; no date last")}
              <th title="Your position delta and theta in this underlying">Position</th>
              <th className="rm-col" aria-label="Remove" />
            </tr>
          </thead>
          <tbody>
            {sorted.map((r, i) => {
              // Sorted by earnings: a heading for each day and side of the bell.
              const group = sort.key === "earnings" ? earningsGroup(r) : null;
              const heading = group !== null && (i === 0 || earningsGroup(sorted[i - 1]) !== group);
              return (
              <Fragment key={r.symbol}>
              {heading ? (
                <tr className="day-row">
                  <td className="l" colSpan={18}>
                    <span>{group}</span>
                  </td>
                </tr>
              ) : null}
              <tr
                className={`strat ${removing.has(r.symbol) ? "removing" : ""}`}
                tabIndex={0}
                onClick={() => (window.location.hash = `#/trade/${encodeURIComponent(r.symbol)}`)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") window.location.hash = `#/trade/${encodeURIComponent(r.symbol)}`;
                }}
              >
                <td className="l">
                  <span className="sym-inline">{r.symbol}</span>
                  {r.extreme?.direction ? (
                    <span
                      className={`extreme ${r.extreme.direction}`}
                      title={`Price extreme: ${r.extreme.reasons.join("; ")}`}
                    >
                      {r.extreme.direction === "down" ? "▼" : "▲"}
                    </span>
                  ) : null}
                  {r.shock ? (
                    <span
                      className="shock"
                      title={`${r.shock}. The scanner opens only defined-risk spreads here, at half size, for now: part of the implied volatility is pricing real risk of another gap.`}
                    >
                      ⚡
                    </span>
                  ) : null}
                  {r.error ? (
                    <span className="sub" title={r.error}>
                      no options data
                    </span>
                  ) : null}
                </td>
                <td>{num(r.price, 2)}</td>
                <td className={tone(r.change_pct)}>
                  {r.change_pct === null ? "–" : `${signed(r.change_pct, 2)}%`}
                  {r.extreme?.day_ratio !== null && r.extreme?.day_ratio !== undefined && r.extreme.reasons.some((x) => x.startsWith("today")) ? (
                    <span className="sub warn-text"> {r.extreme.day_ratio.toFixed(1)}× avg</span>
                  ) : null}
                </td>
                <td
                  title={
                    r.extreme && r.extreme.high !== null && r.extreme.low !== null
                      ? `52-week high ${r.extreme.high.toFixed(2)}\n52-week low ${r.extreme.low.toFixed(2)}\n` +
                        `Now ${r.price === null ? "–" : r.price.toFixed(2)}, ${Math.round((r.extreme.range_pos ?? 0) * 100)}% of the range (closing prices)`
                      : "52-week range not read yet"
                  }
                >
                  <RangeBar x={r.extreme} />
                </td>
                <td className={r.extreme?.reasons.some((x) => x.includes("-day move")) ? "warn-text" : ""}>
                  {r.extreme?.move_z === null || r.extreme?.move_z === undefined
                    ? "–"
                    : `${signed(r.extreme.move_z, 1)}σ`}
                  {r.extreme?.move_z !== null && r.extreme?.move_z !== undefined ? (
                    <span className="sub"> {r.extreme.move_days}d</span>
                  ) : null}
                </td>
                <td>{num(r.iv30, 1)}</td>
                <td
                  className={r.ivr?.rank !== null && r.ivr?.rank !== undefined && r.ivr.rank >= 50 ? "hi" : ""}
                  title={ivrTitle(r.ivr)}
                >
                  {r.ivr?.rank !== null && r.ivr?.rank !== undefined ? Math.round(r.ivr.rank) : "–"}
                  {r.ivr && r.ivr.source === "recorded" && r.ivr.days !== null && r.ivr.days < 252 ? (
                    <span className="sub"> ({r.ivr.days}d)</span>
                  ) : null}
                  {r.ivr?.source === "DoltHub" ? <span className="sub"> D</span> : null}
                  {r.ivr?.source.startsWith("Cboe ") ? <span className="sub"> C</span> : null}
                </td>
                <td>{r.ivr?.percentile !== null && r.ivr?.percentile !== undefined ? Math.round(r.ivr.percentile) : "–"}</td>
                <td>{r.skew === null || r.skew === undefined ? "–" : num(r.skew, 1)}</td>
                <td>
                  <Stars
                    n={r.liquidity ?? null}
                    title={liquidityTitle(
                      { stars: r.liquidity ?? null, spread_pct: r.spread_pct, width: r.spread_width ?? null, oi: r.atm_oi },
                      r.expiration,
                    )}
                  />
                </td>
                <td>
                  {r.expected_move === null ? "–" : `±${num(r.expected_move, 2)}`}
                  {r.expected_move && r.price ? (
                    <span className="sub"> {pct(r.expected_move / r.price, 1)}</span>
                  ) : null}
                </td>
                <td>{r.dte === null ? "–" : `${r.dte}d`}</td>
                <td className={r.spread_pct !== null && r.spread_pct > 0.05 ? "warn-text" : ""}>
                  {pct(r.spread_pct, 1)}
                </td>
                <td>{r.atm_oi === null ? "–" : r.atm_oi.toLocaleString("en-US")}</td>
                <td
                  className={r.float?.low ? "hi" : ""}
                  title={
                    r.float
                      ? `${r.float.shares.toLocaleString("en-US", { maximumFractionDigits: 0 })} shares in the float` +
                        (r.float.pct_of_outstanding !== null ? `, ${pct(r.float.pct_of_outstanding, 0)} of all shares` : "") +
                        (r.float.turnover !== null ? `. ${pct(r.float.turnover, 1)} of it trades on an average day` : "") +
                        (r.float.low ? ". Low float" : "")
                      : undefined
                  }
                >
                  {r.float ? shares(r.float.shares) : "–"}
                  {r.float?.turnover !== null && r.float?.turnover !== undefined ? (
                    <span className="sub"> {pct(r.float.turnover, 1)}/d</span>
                  ) : null}
                </td>
                <td
                  className={`l ${r.fund ? "" : "earn-cell"}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    if (!r.fund) setEditingEarn(editingEarn === r.symbol ? null : r.symbol);
                  }}
                  onKeyDown={(e) => e.stopPropagation()}
                  title={r.fund ? "A fund or index: no earnings" : "Tap to set the date yourself"}
                >
                  {r.earnings ? (
                    <>
                      <EarningsFlag e={r.earnings} tag={false} />
                      {r.earnings_mine ? <span className="sub"> yours</span> : null}
                    </>
                  ) : r.fund ? (
                    <span className="dim-text">–</span>
                  ) : r.earnings_quiet ? (
                    <span
                      className="dim-text"
                      title={`Reported ${shortDay(r.earnings_quiet.last)}; no next date out yet. Reports come about 90 days apart, so the scanner assumes none before ${shortDay(r.earnings_quiet.until)} and opens only trades that expire before then. Tap to set the date yourself.`}
                    >
                      Reported {shortDay(r.earnings_quiet.last)} · clear to {shortDay(r.earnings_quiet.until)}
                    </span>
                  ) : (
                    <span className="warn-text">No date · set</span>
                  )}
                </td>
                <td>
                  {r.held ? (
                    <>
                      <span className={tone(r.position_delta)}>Δ {signed(r.position_delta)}</span>
                      <span className="sub"> Θ {signed(r.position_theta)}</span>
                    </>
                  ) : (
                    ""
                  )}
                </td>
                <td className="rm-col" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                  <button
                    className="link rm"
                    title={`Remove ${r.symbol} from the watchlist`}
                    aria-label={`Remove ${r.symbol}`}
                    disabled={busy}
                    onClick={() => remove(r.symbol)}
                  >
                    ×
                  </button>
                </td>
              </tr>
              {editingEarn === r.symbol ? (
                <tr className="day-row edit-row">
                  <td className="l" colSpan={18}>
                    <EarningsEditor
                      row={r}
                      onDone={(changed) => {
                        setEditingEarn(null);
                        if (changed) load();
                      }}
                    />
                  </td>
                </tr>
              ) : null}
              </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
