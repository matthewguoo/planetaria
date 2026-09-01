/** Shared display formatters — the one home for money, P/L color, and
 * timestamps. Four fmtUsd implementations used to disagree on where the
 * minus sign went (STRATEGIES rendered "$-1,234"); this is the one that
 * renders "-$1,234" everywhere. */

/** "$1,234" (whole dollars, sign-aware; "+$…" when sign=true); "—" unknown. */
export const fmtUsd = (v: number | null | undefined, sign = false) =>
  v === null || v === undefined
    ? "—"
    : `${sign && v > 0 ? "+" : ""}${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;

/** Tailwind text class for a signed P/L value. */
export const pnlCls = (v: number | null | undefined) =>
  v === null || v === undefined ? "text-bb-muted" : v >= 0 ? "text-bb-profit" : "text-bb-loss";

/** "63%" from a 0–1 probability; "—" unknown. */
export const pct = (v: number | null) => (v === null ? "—" : `${(v * 100).toFixed(0)}%`);

/** Signed whole-dollar amount: "+$12" / "-$3". */
export const usd = (v: number) => `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(0)}`;

/** "HH:MM:SS" clipped from an ISO timestamp — shown in the timestamp's own
 * zone (the backend journals UTC); "—" when absent. */
export const hhmmss = (iso: string | null | undefined) => (iso ? iso.slice(11, 19) : "—");

/** "MM-DD HH:MM" clipped from an ISO timestamp; "—" when absent. */
export const dateTimeShort = (iso: string | null | undefined) =>
  iso ? iso.slice(5, 16).replace("T", " ") : "—";

/** "Mon D, HH:MM" in ET for an ISO instant; "—" when absent. */
export function etDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Local wall-clock "HH:MM:SS" from an epoch-seconds stamp. */
export function clockHms(epochS: number): string {
  const t = new Date(epochS * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`;
}
