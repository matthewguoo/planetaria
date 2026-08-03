/** Shared display formatters. One definition each for the money/percent
 * strings that repeat across desktop and mobile panes. */

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
