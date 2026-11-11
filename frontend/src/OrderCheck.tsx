import { Fragment, useEffect, useState } from "react";
import { getJson, postJson } from "./api";
import { clock } from "./format";

type Diff = { field: string; schwab: unknown; app: unknown; level: "differs" | "note"; note: string | null };

type Entry = {
  order_id: number;
  entered_at: string | null;
  status: string | null;
  underlying: string | null;
  legs: string[];
  result: "match" | "differs" | "not comparable";
  reason: string | null;
  diffs: Diff[];
  schwab: unknown;
  app: unknown;
};

type View = { entries: Entry[]; counts: Record<string, number>; checked_at: number | null; error: string | null };

const show = (v: unknown) => (v === null || v === undefined ? "–" : typeof v === "string" ? v : JSON.stringify(v));

function entered(s: string | null): string {
  if (!s) return "–";
  const d = new Date(s.replace(/\+0000$/, "Z"));
  return isNaN(d.getTime())
    ? s
    : d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/** Orders placed in thinkorswim, each compared with the order the app would build. */
export default function OrderCheck() {
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<number | null>(null);

  useEffect(() => {
    getJson<View>("/api/order-check").then(setView, (e) => setError(String(e.message)));
  }, []);

  const refresh = async () => {
    setBusy(true);
    setError(null);
    try {
      setView(await postJson<View>("/api/order-check/refresh", {}));
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const c = view?.counts || {};
  return (
    <>
      <h3>Order check: thinkorswim vs the app</h3>
      <div className="toolbar">
        <span className="dim-text">
          Place an order in thinkorswim at a price that can't fill, then cancel it. Each order on the account is
          rebuilt by the app from the same legs and price, and the two are compared.
          {view ? (
            <>
              {" "}
              {c.match || 0} match · {c.differs || 0} differ · {c["not comparable"] || 0} not comparable · checked{" "}
              {view.checked_at ? clock(view.checked_at) : "not yet"}.
            </>
          ) : null}
        </span>
        <button type="button" className="small" disabled={busy} onClick={refresh}>
          {busy ? "Checking…" : "Check now"}
        </button>
      </div>
      {error || view?.error ? <div className="msg warn">{error || view?.error}</div> : null}
      {view && view.entries.length ? (
        <div className="table-wrap">
          <table className="positions orders">
            <thead>
              <tr>
                <th className="l">Entered</th>
                <th className="l">Order</th>
                <th className="l">Legs</th>
                <th className="l">Status</th>
                <th className="l">Result</th>
              </tr>
            </thead>
            <tbody>
              {view.entries.map((e) => (
                <Fragment key={e.order_id}>
                  <tr className="strat" tabIndex={0} onClick={() => setOpen(open === e.order_id ? null : e.order_id)}>
                    <td className="l">{entered(e.entered_at)}</td>
                    <td className="l">
                      {e.underlying} <span className="sub">#{e.order_id}</span>
                    </td>
                    <td className="l mono-small">{e.legs.join(" · ")}</td>
                    <td className="l">{e.status?.toLowerCase()}</td>
                    <td className="l">
                      <span className={`check-result ${e.result.replace(" ", "-")}`}>{e.result}</span>
                      {e.result === "differs" ? (
                        <span className="sub"> {e.diffs.filter((d) => d.level === "differs").map((d) => d.field).join(", ")}</span>
                      ) : null}
                      {e.reason ? <span className="sub"> {e.reason}</span> : null}
                    </td>
                  </tr>
                  {open === e.order_id ? (
                    <tr className="leg">
                      <td className="l" colSpan={5}>
                        {e.diffs.length ? (
                          <table className="check-diffs">
                            <thead>
                              <tr>
                                <th className="l">Field</th>
                                <th className="l">thinkorswim (Schwab's record)</th>
                                <th className="l">App</th>
                                <th className="l"></th>
                              </tr>
                            </thead>
                            <tbody>
                              {e.diffs.map((d, i) => (
                                <tr key={i} className={d.level === "differs" ? "warn-text" : "dim-text"}>
                                  <td className="l">{d.field}</td>
                                  <td className="l">{show(d.schwab)}</td>
                                  <td className="l">{show(d.app)}</td>
                                  <td className="l">{d.note || ""}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        ) : e.result === "match" ? (
                          <p className="dim-text">Every field the app sends is the same.</p>
                        ) : null}
                        <div className="json-pair">
                          <div>
                            <div className="sub">Schwab's record of the thinkorswim order</div>
                            <pre>{JSON.stringify(e.schwab, null, 2)}</pre>
                          </div>
                          <div>
                            <div className="sub">What the app would send</div>
                            <pre>{e.app ? JSON.stringify(e.app, null, 2) : "Not built: " + (e.reason || "")}</pre>
                          </div>
                        </div>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      ) : view ? (
        <p className="dim-text">No orders on the account in the last 30 days.</p>
      ) : null}
    </>
  );
}
