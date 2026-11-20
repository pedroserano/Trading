import type { IvRank } from "./types";
const DASH = "–";

export function money(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return DASH;
  return v.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function signed(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return DASH;
  const s = money(Math.abs(v), digits);
  return v > 0 ? `+${s}` : v < 0 ? `−${s}` : s;
}

export function pct(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return DASH;
  return `${(v * 100).toFixed(digits)}%`;
}

export function num(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return DASH;
  return v.toFixed(digits);
}

export function tone(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return "";
  return v > 0 ? "pos" : "neg";
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function shortDate(iso: string | null): string {
  if (!iso) return DASH;
  const [y, m, d] = iso.split("-").map(Number);
  // Another year's date carries its last two digits, so Jan 2027 can't pass for Jan 2026.
  const year = y !== new Date().getFullYear() ? ` '${String(y).slice(-2)}` : "";
  return `${MONTHS[m - 1]} ${d}${year}`;
}

export function strike(v: number | null): string {
  if (v === null) return DASH;
  return Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/0$/, "");
}

export function clock(epochSeconds: number | null): string {
  if (!epochSeconds) return DASH;
  return new Date(epochSeconds * 1000).toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Where an IV rank comes from, for its tooltip. */
export function ivrTitle(v: IvRank | null | undefined): string {
  if (!v) return "No IV rank yet";
  if (v.source === "DoltHub")
    return (
      `IV rank from DoltHub as of ${v.as_of}: IV ${v.iv}% in a 52-week range of ${v.low}–${v.high}%. ` +
      `Used until this app has a year of its own IV` +
      (v.recorded_days ? ` (${v.recorded_days} days so far).` : ".")
    );
  if (v.source.startsWith("Cboe "))
    return (
      `IV rank from ${v.source} (Cboe's volatility index for it) as of ${v.as_of}: ${v.iv}% in a 52-week range of ${v.low}–${v.high}%. ` +
      `Used until this app has a year of its own IV` +
      (v.recorded_days ? ` (${v.recorded_days} days so far).` : ".")
    );
  if (v.source === "VIX") return `IV rank over ${v.days} days of VIX; percentile ${v.percentile}`;
  return `IV rank over ${v.days} days of recorded IV` + (v.percentile !== null ? `; percentile ${v.percentile}` : "");
}
