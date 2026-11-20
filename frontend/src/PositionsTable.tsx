import { Fragment, useMemo, useState, type ReactNode } from "react";
import { AutoBadge } from "./Automation";
import EarningsFlag from "./Earnings";
import { Actions, Flags, LotLine } from "./PositionParts";
import { isShares, mergeShares, sharesText } from "./shares";
import { ivrTitle, money, num, pct, shortDate, signed, strike, tone } from "./format";
import type {
  EarningsInfo,
  LegView,
  Snapshot,
  StrategyView,
  UnderlyingView,
} from "./types";
import { useMediaQuery } from "./useMediaQuery";

/** The table's columns. Every row type renders from the same list, so they always line up. */
type Col =
  | "name"
  | "exp"
  | "dte"
  | "qty"
  | "cost"
  | "mark"
  | "pl"
  | "day"
  | "pct"
  | "dbw"
  | "theta"
  | "vega"
  | "gamma"
  | "shortd"
  | "maxloss"
  | "flags";

const GREEKS: Col[] = ["dbw", "theta", "vega", "gamma"];
const COLS: Record<"compact" | "simple" | "detail", Col[]> = {
  // Phones: how the trade is doing first; cost and Greeks further right (the table scrolls).
  compact: ["name", "dte", "pl", "day", "pct", "cost", ...GREEKS],
  simple: [
    "name",
    "exp",
    "dte",
    "cost",
    "pl",
    "day",
    "pct",
    ...GREEKS,
    "flags",
  ],
  detail: [
    "name",
    "exp",
    "dte",
    "qty",
    "cost",
    "mark",
    "pl",
    "day",
    "pct",
    ...GREEKS,
    "shortd",
    "maxloss",
    "flags",
  ],
};

const HEAD: Record<Col, [string, string?]> = {
  name: ["Strategy"],
  exp: ["Exp"],
  dte: ["DTE", "Days to expiration"],
  qty: ["Qty"],
  cost: [
    "Cost basis",
    "What it was opened at, per unit: the credit taken in (cr) or the debit paid (db) per contract; for shares, the average price paid",
  ],
  mark: ["Mark", "Current price per unit"],
  pl: ["P/L"],
  day: [
    "Day",
    "Today's P/L: held overnight from yesterday's close, opened today from the fill",
  ],
  pct: ["% max", "P/L as a share of max profit"],
  dbw: [
    "β-Δ",
    "Beta-weighted delta in SPY shares: each position's part of the header's β-Δ",
  ],
  theta: ["Θ", "Theta: dollars a day from time decay"],
  vega: [
    "Vega",
    "Dollars gained (+) or lost (−) for each one-point rise in implied volatility",
  ],
  gamma: [
    "Γ",
    "Gamma: shares of delta added for each $1 rise in the underlying (not beta-weighted)",
  ],
  shortd: ["Short Δ", "Delta of each short option"],
  maxloss: ["Max loss"],
  flags: ["Flags"],
};

/** Where the summed figures start: what comes before is the name and descriptive columns. */
const SUMMED: Col[] = ["pl", "day", ...GREEKS];
function leadOf(cols: Col[]): number {
  return cols.findIndex((c) => SUMMED.includes(c));
}

/** Gamma is small: a decimal under 10. */
function greek(v: number | null | undefined, c: Col): string {
  if (v === null || v === undefined) return "–";
  const d = c === "gamma" && Math.abs(v) < 10 ? 1 : 0;
  // Nothing that rounds to zero shows a sign.
  return Math.abs(v) < 0.5 * 10 ** -d ? "0" : signed(v, d);
}

interface Row {
  under: UnderlyingView;
  st: StrategyView;
}

type View = "underlying" | "dte";

export default function PositionsTable(props: {
  s: Snapshot;
  view: View;
  flaggedOnly: boolean;
  detail: boolean;
}) {
  const { s, view, flaggedOnly, detail } = props;
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
  // Shares bought in several orders show as one row per underlying.
  const unders = useMemo(
    () =>
      s.underlyings.map((u) => ({
        ...u,
        strategies: mergeShares(u.strategies),
      })),
    [s],
  );
  const flat: Row[] = useMemo(
    () =>
      unders
        .flatMap((u) => u.strategies.map((st) => ({ under: u, st })))
        .filter((r) => filter(r.st))
        .sort(
          (a, b) => (a.st.numbers.dte ?? 9999) - (b.st.numbers.dte ?? 9999),
        ),
    [unders, flaggedOnly],
  );

  if (s.underlyings.length === 0) {
    return <div className="empty">{s.needs ? "" : "No open positions."}</div>;
  }
  const cols = COLS[compact ? "compact" : detail ? "detail" : "simple"];
  const showUnder = view === "dte";
  const t = s.totals;
  const totals: Partial<Record<Col, number | null | undefined>> = {
    pl: t.pl,
    day: t.day_pl,
    dbw: t.delta_bw,
    theta: t.theta,
    vega: t.vega,
    gamma: t.gamma,
  };
  return (
    <div className="table-wrap">
      <table className={`positions pos-table ${compact ? "compact" : ""}`}>
        <thead>
          <tr>
            {cols.map((c) => (
              <th
                key={c}
                className={`c-${c} ${c === "name" || c === "flags" ? "l" : ""}`}
                title={HEAD[c][1]}
              >
                {c === "name" && showUnder && !compact
                  ? "Underlying / strategy"
                  : HEAD[c][0]}
              </th>
            ))}
          </tr>
          {/* The account's totals, the same figures as the header, over each column. */}
          <tr className="totals">
            {cols.map((c) =>
              c === "name" ? (
                <td
                  key={c}
                  className="l"
                  title="The whole account: the figures in the header"
                >
                  Total
                </td>
              ) : c in totals ? (
                <td
                  key={c}
                  className={c === "pl" || c === "day" ? tone(totals[c]) : ""}
                >
                  {c === "pl" || c === "day"
                    ? signed(totals[c] ?? null)
                    : greek(totals[c], c)}
                </td>
              ) : (
                <td key={c} />
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {view === "underlying"
            ? unders.map((u) => {
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
                    detail={detail}
                    cols={cols}
                  />
                );
              })
            : flat.map((r, i) => (
                <StrategyRows
                  key={`${r.under.symbol}-${i}`}
                  id={`${r.under.symbol}-${r.st.name}-${r.st.numbers.expiration}-${i}`}
                  st={r.st}
                  under={r.under.symbol}
                  earnings={r.under.earnings}
                  open={open}
                  toggle={toggle}
                  compact={compact}
                  cols={cols}
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
  detail: boolean;
  cols: Col[];
}) {
  const { u, detail, cols } = props;
  const lead = leadOf(cols);
  const sub: Partial<Record<Col, number | null | undefined>> = {
    pl: u.pl,
    day: u.day_pl,
    dbw: u.delta_bw,
    theta: u.theta,
    vega: u.vega,
    gamma: u.gamma,
  };
  return (
    <>
      <tr className="under">
        <td className="l" colSpan={lead}>
          <a
            className="sym"
            href={`#/trade/${encodeURIComponent(u.symbol)}`}
            onClick={(e) => e.stopPropagation()}
          >
            {u.symbol}
          </a>
          {/* Break points, so a narrow phone wraps the change under the price. */}
          <wbr />
          <span className="px">{num(u.price, 2)}</span>
          <wbr />
          <span className="chg">
            {u.change_pct === null ? "" : `${signed(u.change_pct, 2)}%`}
          </span>
          {detail && u.ivr && u.ivr.rank !== null ? (
            <span
              className="ivr"
              title={ivrTitle(u.ivr)}
            >
              IVR {Math.round(u.ivr.rank)}
              {u.ivr.source === "recorded" && u.ivr.days !== null && u.ivr.days < 252 ? (
                <span className="sub"> ({u.ivr.days}d)</span>
              ) : null}
              {u.ivr.source === "DoltHub" ? <span className="sub"> D</span> : null}
            </span>
          ) : null}
          {detail && u.beta !== null ? (
            <span className="beta">β {u.beta.toFixed(2)}</span>
          ) : null}
          <EarningsFlag e={u.earnings} />
        </td>
        {cols.slice(lead).map((c) =>
          c === "pl" || c === "day" ? (
            <td key={c} className={tone(sub[c])}>
              {signed(sub[c] ?? null)}
            </td>
          ) : c in sub ? (
            <td
              key={c}
              title={
                c === "dbw"
                  ? `Delta ${signed(u.delta, 0)} shares of ${u.symbol}`
                  : undefined
              }
            >
              {greek(sub[c], c)}
            </td>
          ) : (
            <td key={c} />
          ),
        )}
      </tr>
      {props.sts.map((st, i) => (
        <StrategyRows
          key={i}
          id={`${u.symbol}-${st.name}-${st.numbers.expiration}-${i}`}
          st={st}
          open={props.open}
          toggle={props.toggle}
          compact={props.compact}
          cols={cols}
          underSymbol={u.symbol}
        />
      ))}
    </>
  );
}

function perUnit(st: StrategyView, total: number | null): number | null {
  const n = st.numbers;
  if (
    total === null ||
    n.contracts === 0 ||
    st.legs.some((l) => l.kind === "EQUITY")
  )
    return null;
  return Math.abs(total) / (n.contracts * 100);
}

/** Cost basis per unit: the credit (cr) or debit (db) per contract, or shares' average price. */
/** Shares' average price, and what covered calls (covered puts, on short shares) have done
 * to it: premium taken in lowers the basis, a buyback or roll for a debit raises it. */
function sharesBasis(st: StrategyView) {
  const q = st.legs.reduce((a, l) => a + l.quantity, 0);
  const cost = st.legs.reduce((a, l) => a + l.quantity * l.avg_price, 0);
  const n = st.numbers;
  return {
    q,
    cost,
    avg: q ? cost / q : null,
    adjusted: n.adjusted_basis ?? null,
    premium: n.covered_premium ?? 0,
    count: n.covered_count ?? 0,
    since: n.covered_since ?? null,
  };
}

function basisTitle(st: StrategyView): string {
  const b = sharesBasis(st);
  const short = b.q < 0;
  const paid = `${short ? "Sold at" : "Paid"} ${num(b.avg, 2)} a share, ${money(Math.abs(b.cost))} in all.`;
  if (b.adjusted === null || !b.q) return paid;
  const what = short ? "covered puts" : "covered calls";
  const since = b.since ? ` since ${shortDate(new Date(b.since * 1000).toISOString().slice(0, 10))}` : "";
  return (
    `${paid}\n${b.count} ${b.count === 1 ? what.slice(0, -1) : what}${since} have netted ` +
    `${signed(b.premium)} (${signed(b.premium / Math.abs(b.q), 2)} a share), so ` +
    `${short ? "effectively sold at" : "adjusted basis"} ${num(b.adjusted, 2)}.`
  );
}

function CostCell({ st }: { st: StrategyView }) {
  const n = st.numbers;
  if (isShares(st)) {
    const b = sharesBasis(st);
    return (
      <td title={basisTitle(st)}>
        {num(b.adjusted ?? b.avg, 2)}
        {b.adjusted !== null ? <span className="sub"> adj</span> : null}
      </td>
    );
  }
  const px = perUnit(st, n.open_value);
  return (
    <td title={`Cost basis: ${n.credit ? "credit taken in" : "debit paid"}, ${money(Math.abs(n.open_value))} in all`}>
      {px === null ? money(Math.abs(n.open_value)) : num(px, 2)}
      <span className="sub"> {n.credit ? "cr" : "db"}</span>
    </td>
  );
}

function StrategyRows(props: {
  id: string;
  st: StrategyView;
  under?: string;
  open: Set<string>;
  toggle: (k: string) => void;
  compact: boolean;
  cols: Col[];
  underSymbol?: string;
  earnings?: EarningsInfo | null;
}) {
  const { st, id, compact, cols } = props;
  const n = st.numbers;
  const expanded = props.open.has(id);
  const shares = isShares(st);
  const stockLeg = shares ? st.legs[0] : null;
  const undefinedRisk =
    !shares &&
    !st.defined_risk &&
    n.covered_by !== "ZEBRA" &&
    st.legs.some((l) => l.quantity < 0);
  const title = shares ? sharesText(st, false) : st.name;

  const name = compact ? (
    <>
      {/* Phones: the name on one line, everything about it on the next. */}
      <span className="nm">
        <span className="caret">{expanded ? "▾" : "▸"}</span>
        {props.under ? <span className="sym-inline">{props.under}</span> : null}
        {title}
        {st.account_name ? <span className="acct-tag">{st.account_name}</span> : null}
      </span>
      <div className="meta">
        {st.lots ? (
          <span className="sub">{st.lots.length} purchases</span>
        ) : null}
        {!shares && (n.contracts || n.expiration) ? (
          <span className="sub">
            {n.contracts ? `×${n.contracts} ` : ""}
            {n.expiration ? shortDate(n.expiration) : ""}
          </span>
        ) : null}
        {n.covered_by === "ZEBRA" ? (
          <span className="sub">on ZEBRA</span>
        ) : null}
        {n.shares_like ? (
          <span className="sub">
            ≈{n.shares_like < 0 ? "−" : ""}
            {Math.abs(n.shares_like)} sh
          </span>
        ) : null}
        {undefinedRisk ? (
          <span className="undef" title="Undefined risk">
            U
          </span>
        ) : null}
        <AutoBadge st={st} />
        {st.flags.length ? <Flags st={st} /> : null}
      </div>
    </>
  ) : (
    <>
      <span className="caret">{expanded ? "▾" : "▸"}</span>
      {props.under ? <span className="sym-inline">{props.under}</span> : null}
      {title}
      {st.account_name ? <span className="acct-tag">{st.account_name}</span> : null}
      {st.lots ? (
        <span className="sub"> · {st.lots.length} purchases</span>
      ) : null}
      {n.covered_by === "ZEBRA" ? (
        <span
          className="sub"
          title={`Written against a ZEBRA held on this underlying, expiring ${n.cover_until ?? "–"}: automanage never rolls it past that date`}
        >
          {" "}
          on ZEBRA
        </span>
      ) : null}
      {n.shares_like ? (
        <span
          className="sub"
          title="A ZEBRA moves like stock: 100 shares per unit"
        >
          {" "}
          ≈{n.shares_like < 0 ? "−" : ""}
          {Math.abs(n.shares_like)} sh
        </span>
      ) : null}
      {props.under ? <EarningsFlag e={props.earnings} /> : null}
      {undefinedRisk ? (
        <span className="undef" title="Undefined risk">
          U
        </span>
      ) : null}
      <AutoBadge st={st} />
    </>
  );

  const cell = (c: Col): ReactNode => {
    switch (c) {
      case "name":
        return (
          <td key={c} className="l name">
            {name}
          </td>
        );
      case "exp":
        return <td key={c}>{shares ? "" : shortDate(n.expiration)}</td>;
      case "dte":
        return <td key={c}>{shares ? "" : (n.dte ?? "–")}</td>;
      case "qty":
        return <td key={c}>{shares ? "" : n.contracts || "–"}</td>;
      case "cost":
        return <CostCell key={c} st={st} />;
      case "mark": {
        const px = perUnit(st, n.value);
        return (
          <td key={c}>
            {shares
              ? num(stockLeg?.mark ?? null, 2)
              : px === null
                ? money(n.value === null ? null : Math.abs(n.value))
                : num(px, 2)}
          </td>
        );
      }
      case "pl":
        return (
          <td key={c} className={tone(n.pl)}>
            {signed(n.pl)}
          </td>
        );
      case "day":
        return (
          <td key={c} className={tone(n.day_pl)}>
            {signed(n.day_pl)}
          </td>
        );
      case "pct":
        return (
          <td key={c}>
            {n.pct_max_profit === null || shares ? "" : pct(n.pct_max_profit)}
          </td>
        );
      case "dbw":
        return (
          <td key={c} title={`Delta ${signed(n.delta, 0)} shares`}>
            {greek(n.delta_bw, c)}
          </td>
        );
      case "theta":
        return <td key={c}>{shares ? "" : greek(n.theta, c)}</td>;
      case "vega":
        return <td key={c}>{shares ? "" : greek(n.vega, c)}</td>;
      case "gamma":
        return <td key={c}>{shares ? "" : greek(n.gamma, c)}</td>;
      case "shortd":
        return (
          <td key={c}>
            {n.short_deltas
              .map((d) => (d === null ? "–" : Math.abs(d).toFixed(2)))
              .join(" / ") || (shares ? "" : "–")}
          </td>
        );
      case "maxloss":
        return (
          <td key={c}>
            {shares
              ? ""
              : n.max_loss === null
                ? st.defined_risk || n.covered_by === "ZEBRA"
                  ? "–"
                  : "∞"
                : money(n.max_loss)}
          </td>
        );
      case "flags":
        return (
          <td key={c} className="l">
            <Flags st={st} />
          </td>
        );
    }
  };

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
        {cols.map(cell)}
      </tr>
      {expanded && st.lots
        ? st.lots.map((lot, i) => (
            <tr className="leg" key={`lot-${i}`}>
              <td className="l" colSpan={cols.length}>
                <LotLine lot={lot} />
              </td>
            </tr>
          ))
        : null}
      {expanded && !st.lots
        ? st.legs.map((l) => <LegRow key={l.symbol} l={l} cols={cols} />)
        : null}
      {expanded && st.legs.length && (props.under ?? props.underSymbol) ? (
        <tr className="leg actions">
          <td className="l" colSpan={cols.length}>
            <div className="pos-actions">
              <Actions st={st} under={props.under ?? props.underSymbol} />
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}

/** One leg: its own figures up to P/L, then a line of per-share numbers. */
function LegRow({ l, cols }: { l: LegView; cols: Col[] }) {
  const lead = leadOf(cols);
  const head = cols.slice(0, lead);
  const bits = [
    head.includes("cost") ? null : `open ${num(l.avg_price, 2)}`,
    head.includes("mark") ? null : `mark ${num(l.mark, 2)}`,
    l.kind === "OPTION" ? `Δ ${num(l.delta, 2)}` : null,
    l.kind === "OPTION" && l.theta !== null && l.theta !== undefined
      ? `Θ ${num(l.theta, 3)}`
      : null,
    l.iv === null || l.iv === undefined ? null : `IV ${l.iv.toFixed(1)}`,
  ].filter(Boolean);
  return (
    <tr className="leg">
      {head.map((c) => (
        <Fragment key={c}>
          {c === "name" ? (
            <td className="l">
              <span className={tone(l.quantity)}>{signed(l.quantity)}</span>{" "}
              {l.kind === "OPTION"
                ? `${shortDate(l.expiration)} ${strike(l.strike)} ${l.put_call === "P" ? "Put" : "Call"}`
                : "Shares"}
            </td>
          ) : c === "dte" ? (
            <td>{l.dte ?? ""}</td>
          ) : c === "cost" ? (
            <td>{num(l.avg_price, 2)}</td>
          ) : c === "mark" ? (
            <td>{num(l.mark, 2)}</td>
          ) : (
            <td />
          )}
        </Fragment>
      ))}
      <td className="l sub leg-figs" colSpan={cols.length - lead}>
        {bits.join(" · ")}
      </td>
    </tr>
  );
}
