/**
 * Shared phone primitives: the finger-sized button, the ± stepper, the
 * protection dot, and the one-line account strip. Numbers, not prose.
 */

import type { Protection } from "../../lib/planRisk";
import { useAccountStore } from "../../store/accountStore";

/** Big touch button; `danger` = red fill, `primary` = amber fill. */
export function Btn({
  children, onClick, kind = "ghost", disabled, className = "",
}: {
  children: React.ReactNode; onClick: () => void; kind?: "ghost" | "danger" | "primary" | "outline-danger";
  disabled?: boolean; className?: string;
}) {
  const cls = {
    ghost: "border border-bb-border text-bb-muted active:text-bb-amber",
    "outline-danger": "border border-bb-loss text-bb-loss active:bg-bb-loss active:text-black",
    danger: "bg-bb-loss font-semibold text-black active:bg-bb-orange",
    primary: "bg-bb-amber font-semibold text-black active:bg-bb-orange",
  }[kind];
  return (
    <button
      disabled={disabled}
      onClick={onClick}
      className={`h-11 px-3 text-[12px] tracking-wider disabled:opacity-40 ${cls} ${className}`}
    >
      {children}
    </button>
  );
}

export function Stepper({
  label, value, set, step, unit = "", min, max, format,
}: {
  label: string; value: number; set: (v: number) => void; step: number; unit?: string;
  min: number; max?: number; format?: (v: number) => string;
}) {
  const clamp = (v: number) => Math.min(max ?? Infinity, Math.max(min, Number(v.toFixed(2))));
  return (
    <div className="flex items-center justify-between">
      <span className="text-[12px] text-bb-muted">{label}</span>
      <span className="flex items-center gap-1">
        <button className="h-10 w-10 border border-bb-border text-[16px] text-bb-muted active:bg-bb-amber active:text-black" onClick={() => set(clamp(value - step))} aria-label={`${label} down`}>−</button>
        <span data-numeric className="w-20 text-center text-[15px] text-white">{format ? format(value) : `${value}${unit}`}</span>
        <button className="h-10 w-10 border border-bb-border text-[16px] text-bb-muted active:bg-bb-amber active:text-black" onClick={() => set(clamp(value + step))} aria-label={`${label} up`}>+</button>
      </span>
    </div>
  );
}

/** Two-way segmented control. */
export function Seg<T extends string>({ value, options, onChange, danger = false }: {
  value: T; options: readonly { id: T; label: string }[]; onChange: (v: T) => void; danger?: boolean;
}) {
  return (
    <span className="flex gap-px">
      {options.map((o) => (
        <button
          key={o.id}
          onClick={() => onChange(o.id)}
          className={
            "h-10 flex-1 px-3 text-[11px] tracking-widest " +
            (value === o.id
              ? (danger ? "bg-bb-loss" : "bg-bb-amber") + " font-semibold text-black"
              : "border border-bb-border text-bb-muted")
          }
        >
          {o.label}
        </button>
      ))}
    </span>
  );
}

export const PROTECTION_LABEL: Record<Protection, string> = {
  stop: "stop armed",
  premium: "no stop · loss capped at the premium",
  none: "no stop · nothing will exit this",
};

/** Green = stop, amber = premium-capped, red = nothing exits it. */
export function ProtectionDot({ state, size = 2 }: { state: Protection; size?: 2 | 3 }) {
  const cls = state === "stop" ? "bg-bb-profit" : state === "premium" ? "bg-bb-amber" : "bg-bb-loss";
  const dim = size === 3 ? "h-3 w-3" : "h-2 w-2";
  return <span className={`inline-block shrink-0 rounded-full ${dim} ${cls}`} title={PROTECTION_LABEL[state]} aria-label={PROTECTION_LABEL[state]} />;
}

/** One-line book summary: EQUITY · TODAY · CASH. */
export function AccountStrip({ onClick }: { onClick?: () => void }) {
  const account = useAccountStore((s) => s.account);
  const positions = useAccountStore((s) => s.positions);
  const unrealized = positions.reduce((a, p) => a + (p.unrealized_pnl ?? 0), 0);
  const day = (account?.day_realized_pnl ?? 0) + unrealized;
  const cls = day >= 0 ? "text-bb-profit" : "text-bb-loss";
  return (
    <button
      className="flex h-9 w-full shrink-0 items-center gap-4 border-b border-bb-border bg-bb-panel px-3 text-left text-[11px]"
      onClick={onClick}
    >
      <span className="text-bb-muted">
        EQUITY <span data-numeric className="text-[13px] text-white">{account ? `$${Math.round(account.equity).toLocaleString()}` : "—"}</span>
      </span>
      <span className="text-bb-muted">
        TODAY{" "}
        <span data-numeric className={cls}>
          {account ? `${day >= 0 ? "+" : "−"}$${Math.abs(day).toFixed(0)}` : "—"}
          {account && account.equity > 0 ? ` (${day >= 0 ? "+" : "−"}${Math.abs((day / account.equity) * 100).toFixed(2)}%)` : ""}
        </span>
      </span>
      <span className="ml-auto text-bb-muted">
        CASH <span data-numeric className="text-white">{account ? `$${Math.round(account.cash).toLocaleString()}` : "—"}</span>
      </span>
    </button>
  );
}
