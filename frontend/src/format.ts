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
  const [, m, d] = iso.split("-").map(Number);
  return `${MONTHS[m - 1]} ${d}`;
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
