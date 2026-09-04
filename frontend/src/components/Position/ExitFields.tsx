/**
 * The exits of a position as PRICES you type (or drag on the chart — same
 * draft): STOP, TARGET, EXIT day. Chips put the usual distances in with
 * one tap; the loss-at-stop / gain-at-target line does the arithmetic.
 * Shared by the adopt form and a plan's automation editor, phone and
 * desktop.
 */

import { etDateIso } from "../../lib/et";
import { shareExitDayIso } from "../../lib/tradingTime";
import type { ExitDraft } from "../../store/exitDraftStore";

export function PriceField({
  label, value, onChange, touch = false, placeholder = "—", step = 0.01, ariaLabel,
}: {
  label: string; value: number | null; onChange: (v: number | null) => void; touch?: boolean;
  placeholder?: string; step?: number; ariaLabel?: string;
}) {
  return (
    <label className="flex items-center justify-between gap-2">
      <span className={(touch ? "text-[12px]" : "text-[10px]") + " text-bb-muted"}>{label}</span>
      <input
        data-numeric
        type="number"
        inputMode="decimal"
        step={step}
        min={0}
        aria-label={ariaLabel ?? label}
        placeholder={placeholder}
        value={value == null ? "" : value}
        onChange={(e) => {
          const raw = e.target.value;
          if (raw === "") return onChange(null);
          const n = Number(raw);
          if (Number.isFinite(n)) onChange(n);
        }}
        className={
          (touch ? "h-10 w-28 text-[15px]" : "h-7 w-24 text-[12px]") +
          " border border-bb-border bg-black px-2 text-right text-white outline-none focus:border-bb-amber"
        }
      />
    </label>
  );
}

export function DateField({ label, valueUtc, onChange, touch = false }: {
  label: string; valueUtc: string | null; onChange: (utcIso: string | null) => void; touch?: boolean;
}) {
  const ms = valueUtc ? Date.parse(valueUtc) : NaN;
  return (
    <label className="flex items-center justify-between gap-2">
      <span className={(touch ? "text-[12px]" : "text-[10px]") + " text-bb-muted"}>{label}</span>
      <input
        data-numeric
        type="date"
        aria-label={label}
        value={Number.isFinite(ms) ? etDateIso(ms) : ""}
        min={etDateIso()}
        onChange={(e) => {
          const v = e.target.value;
          if (!v) return onChange(null);
          onChange(shareExitDayIso(Date.parse(`${v}T16:00:00Z`)));
        }}
        className={
          (touch ? "h-10 text-[14px]" : "h-7 text-[11px]") +
          " border border-bb-border bg-black px-2 text-white outline-none focus:border-bb-amber"
        }
      />
    </label>
  );
}

function Chip({ on, label, onClick, touch }: { on: boolean; label: string; onClick: () => void; touch: boolean }) {
  return (
    <button
      onClick={onClick}
      className={
        (touch ? "h-8 px-2 text-[11px]" : "h-6 px-1.5 text-[9px]") +
        " tracking-wider " +
        (on ? "bg-bb-amber font-semibold text-black" : "border border-bb-border text-bb-muted hover:text-bb-amber")
      }
    >
      {label}
    </button>
  );
}

const usd = (v: number) => `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(0)}`;

/**
 * `basis` is the absolute entry price/premium, `side` the position's
 * direction, `mult` 100 for contracts. The draft holds signed values (plan
 * convention); this component shows and edits absolute prices.
 */
export function ExitFields({
  kind, side, basis, mult, units, draft, set, touch = false,
  stopRequired = false, targetRemovable = true, timeStop = "date",
}: {
  kind: "option" | "stock";
  side: 1 | -1;
  basis: number;
  mult: number;
  units: number;
  draft: ExitDraft;
  set: (patch: Partial<ExitDraft>) => void;
  touch?: boolean;
  /** Shares: a stop is mandatory (no premium cap to fall back on). */
  stopRequired?: boolean;
  /** A plan's existing target cannot be removed through tighten (only moved). */
  targetRemovable?: boolean;
  /** "date": exit day picker (shares) · "fixed": shown, not edited (options ride to the expiry cutoff) · "none". */
  timeStop?: "date" | "fixed" | "none";
}) {
  const abs = (v: number | null) => (v == null ? null : Math.abs(v));
  const sl = abs(draft.sl);
  const tp = abs(draft.tp);
  const signed = (v: number | null) => (v == null ? null : Number((side * v).toFixed(4)));
  const stopAt = (pct: number) => Number((basis * (1 - side * pct)).toFixed(2));
  const targetAt = (pct: number) => Number((basis * (1 + side * pct)).toFixed(2));
  const stopPcts = kind === "option" ? [0.25, 0.5, 0.75] : [0.05, 0.1, 0.2];
  const targetPcts = kind === "option" ? [0.5, 1, 2] : [0.1, 0.25, 0.5];
  const near = (a: number | null, b: number) => a != null && Math.abs(a - b) < 0.005;
  const lossAtStop = sl == null ? (kind === "option" ? -basis * mult * units : null) : -side * (basis - sl) * mult * units;
  const gainAtTarget = tp == null ? null : side * (tp - basis) * mult * units;
  const stopPct = sl == null ? null : (side * (basis - sl)) / basis;
  const targetPct = tp == null ? null : (side * (tp - basis)) / basis;
  const txt = touch ? "text-[12px]" : "text-[10px]";

  return (
    <div className="flex flex-col gap-2">
      <PriceField label="STOP" value={sl} onChange={(v) => set({ sl: signed(v) })} touch={touch} placeholder={kind === "option" ? "premium" : "required"} />
      <div className="flex flex-wrap gap-1">
        {kind === "option" && !stopRequired && (
          <Chip on={sl == null} label="PREMIUM" onClick={() => set({ sl: null })} touch={touch} />
        )}
        {stopPcts.map((p) => (
          <Chip key={p} on={near(sl, stopAt(p))} label={`−${Math.round(p * 100)}%`} onClick={() => set({ sl: signed(stopAt(p)) })} touch={touch} />
        ))}
      </div>
      <PriceField label="TARGET" value={tp} onChange={(v) => set({ tp: signed(v) })} touch={touch} placeholder="none" />
      <div className="flex flex-wrap gap-1">
        {targetRemovable && <Chip on={tp == null} label="NONE" onClick={() => set({ tp: null })} touch={touch} />}
        {targetPcts.map((p) => (
          <Chip key={p} on={near(tp, targetAt(p))} label={`+${Math.round(p * 100)}%`} onClick={() => set({ tp: signed(targetAt(p)) })} touch={touch} />
        ))}
      </div>
      {timeStop === "date" && (
        <DateField label="EXIT DAY" valueUtc={draft.timeStopUtc} onChange={(v) => set({ timeStopUtc: v })} touch={touch} />
      )}
      {timeStop === "fixed" && (
        <div className={"flex items-center justify-between " + txt}>
          <span className="text-bb-muted">EXIT</span>
          <span data-numeric className="text-white">
            {draft.timeStopUtc
              ? new Date(draft.timeStopUtc).toLocaleString("en-US", { timeZone: "America/New_York", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })
              : "expiry cutoff"}
          </span>
        </div>
      )}
      <div data-numeric className={"flex flex-wrap gap-x-3 text-bb-muted " + (touch ? "text-[12px]" : "text-[11px]")}>
        {lossAtStop != null ? (
          <span>
            {sl == null ? "max loss" : "at stop"} <span className="text-bb-loss">{usd(lossAtStop)}</span>
            {stopPct != null && <span className="ml-1">({(stopPct * 100).toFixed(1)}%)</span>}
          </span>
        ) : (
          <span className="text-bb-loss">a stop is required</span>
        )}
        {gainAtTarget != null && (
          <span>
            at target <span className="text-bb-profit">{usd(gainAtTarget)}</span>
            {targetPct != null && <span className="ml-1">({(targetPct * 100).toFixed(1)}%)</span>}
          </span>
        )}
        {!touch && <span className="text-bb-muted/70">⇕ or drag the lines on the chart</span>}
      </div>
    </div>
  );
}
