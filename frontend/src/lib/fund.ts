/**
 * Pure math for the FUND page's allocation chart, kept out of the component
 * so it can be tested as arithmetic. Segment widths are percentages of the
 * LARGER of account equity and the allocation sum — over-allocation (a
 * dollar allocation typo, an equity drawdown) squeezes every segment
 * proportionally instead of overflowing the bar, and the unallocated
 * remainder only exists when equity genuinely exceeds the allocations.
 */

export type AllocationSegment = {
  id: string;
  label: string;
  value: number;
  pct: number;
  color: string;
};

/** Bloomberg-black friendly, distinct at a glance, stable by index so a
 * strategy keeps its color across refreshes (ordering is the caller's). */
export const FUND_PALETTE = [
  "#f5a623", // amber — the house color leads
  "#4aa3ff",
  "#3ddc84",
  "#ff5c8a",
  "#b48cff",
  "#ffd166",
  "#6ee7dc",
  "#ff8c42",
] as const;

export const UNALLOCATED_COLOR = "#3a3a3a";

export function allocationSegments(
  instances: { id: string; name: string; allocated: number }[],
  equity: number,
): AllocationSegment[] {
  const allocatedTotal = instances.reduce((s, i) => s + Math.max(i.allocated, 0), 0);
  const base = Math.max(equity, allocatedTotal);
  if (base <= 0) return [];
  const segments: AllocationSegment[] = instances
    .filter((i) => i.allocated > 0)
    .map((inst, idx) => ({
      id: inst.id,
      label: inst.name,
      value: inst.allocated,
      pct: (inst.allocated / base) * 100,
      color: FUND_PALETTE[idx % FUND_PALETTE.length],
    }));
  const free = equity - allocatedTotal;
  if (free > 0.005) {
    segments.push({
      id: "__unallocated__",
      label: "unallocated",
      value: free,
      pct: (free / base) * 100,
      color: UNALLOCATED_COLOR,
    });
  }
  return segments;
}
