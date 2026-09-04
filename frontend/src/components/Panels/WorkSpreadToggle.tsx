/**
 * WORK SPREAD on the options ticket: AUTO (follow the server's
 * spread_optimizer setting) / ON / OFF for THIS order. What the choice
 * means is spelled out from the live risk settings — where rung 0 rests,
 * how far and how fast it walks toward the ask, and how the exit ladder
 * prices — so the trader reads the plan, not a mystery toggle. The
 * server stamps the choice on the plan; the exit ladder follows it.
 */

import type { RiskSettings } from "../../lib/api";
import { useAccountStore } from "../../store/accountStore";
import { useStrategyStore } from "../../store/strategyStore";

export function workSpreadEffective(override: boolean | null, risk: RiskSettings | undefined): boolean {
  if (override != null) return override;
  return !!risk?.spread_optimizer;
}

/** One line of plain words for what the optimizer will do with this order. */
export function workSpreadBlurb(risk: RiskSettings | undefined, on: boolean): string {
  if (!on) {
    return "Entry rests at the mid until the entry TTL; exits ladder mid −2% → mid −6% → market.";
  }
  const start = risk?.spread_opt_entry_start ?? 0;
  const step = risk?.spread_opt_entry_step ?? 0.25;
  const max = risk?.spread_opt_entry_max ?? 1;
  const every = risk?.spread_opt_step_s ?? 3;
  const exitMax = risk?.spread_opt_exit_max ?? 1;
  const where = (f: number) =>
    Math.abs(f) < 1e-9 ? "the mid" : f >= 1 ? "the touch" : f > 0 ? `mid +${Math.round(f * 100)}% of the half-spread` : `mid ${Math.round(f * 100)}% of the half-spread`;
  return (
    `Entry starts at ${where(start)}, steps +${Math.round(step * 100)}% of the half-spread every ${every}s ` +
    `up to ${where(max)}, then rests until the entry TTL (never past the price you staged). ` +
    `Exits ladder inside the book (mid → ${exitMax >= 1 ? "the bid" : where(-exitMax).replace("mid ", "mid −")}) then market.`
  );
}

export function WorkSpreadToggle({ touch = false }: { touch?: boolean }) {
  const risk = useAccountStore((s) => s.account?.risk);
  const override = useStrategyStore((s) => s.workSpread);
  const setWorkSpread = useStrategyStore((s) => s.setWorkSpread);
  const serverOn = !!risk?.spread_optimizer;
  const on = workSpreadEffective(override, risk);

  const chip = (active: boolean) =>
    touch
      ? "h-10 shrink-0 border px-3 text-[12px] tracking-wider " +
        (active ? "border-bb-amber bg-bb-amber font-semibold text-black" : "border-bb-border text-bb-muted active:text-bb-amber")
      : "fld-b " + (active ? "on" : "");

  return (
    <div className={touch ? "flex flex-col gap-1" : "flex flex-col gap-0.5"} title={workSpreadBlurb(risk, on)}>
      <div className={touch ? "flex items-center justify-between" : "fld"}>
        <span className={touch ? "text-[12px] text-bb-muted" : "fld-l"}>
          WORK SPREAD{" "}
          <span className={on ? "text-bb-profit" : "text-bb-muted"}>{on ? "ON" : "OFF"}</span>
        </span>
        <span className="flex items-center gap-1">
          <button
            className={chip(override == null)}
            onClick={() => setWorkSpread(null)}
            title={`Follow the server setting (currently ${serverOn ? "ON" : "OFF"})`}
          >
            AUTO
          </button>
          <button className={chip(override === true)} onClick={() => setWorkSpread(true)}>
            ON
          </button>
          <button className={chip(override === false)} onClick={() => setWorkSpread(false)}>
            OFF
          </button>
        </span>
      </div>
      <div className={"leading-tight text-bb-muted " + (touch ? "text-[11px]" : "text-[9px]")}>
        {workSpreadBlurb(risk, on)}
      </div>
    </div>
  );
}
