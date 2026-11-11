import type { EarningsInfo } from "./types";

const SHORT = { bmo: "before open", amc: "after close" } as const;

function day(iso: string, long = false): string {
  const d = new Date(`${iso}T12:00:00`);
  return d.toLocaleDateString("en-US", long ? { weekday: "long", month: "long", day: "numeric" } : { month: "short", day: "numeric" });
}

/** The next earnings report: a flag with the date and before the open or after the close. */
export default function EarningsFlag({ e }: { e: EarningsInfo | null | undefined }) {
  if (!e) return null;
  const near = e.days <= 7;
  const when = e.time ? SHORT[e.time] : "time not given";
  const title =
    `Earnings ${day(e.date, true)}, ${e.label}` +
    (e.days >= 0 ? ` (${e.days === 0 ? "today" : `in ${e.days} day${e.days === 1 ? "" : "s"}`})` : "") +
    `. Last session to be out: ${day(e.exit_by, true)}.` +
    (e.source ? ` Source: ${e.source}.` : "");
  return (
    <span className={`earn ${near ? "near" : ""}`} title={title}>
      <span className="earn-flag" aria-hidden="true">
        ⚑
      </span>
      ER {day(e.date)} · {when}
    </span>
  );
}
