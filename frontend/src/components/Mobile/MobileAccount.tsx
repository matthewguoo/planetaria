/**
 * ACCOUNT on a phone: the numbers as a stat grid, the equity curve, then
 * the persistent settings — capabilities (what this account may trade),
 * the risk rules the server enforces, which broker account is selected —
 * and the closed-trade history. Every control is finger-sized; every
 * change saves on its own button.
 */

import { useState } from "react";
import { apiError, putRisk, type RiskSettings } from "../../lib/api";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
import { capabilitiesSummaryLine } from "../../lib/capabilities";
import { CapabilitiesPanel } from "../Account/CapabilitiesPanel";
import { RiskPresetChips } from "../Account/RiskPresetChips";
import { AccountsPanel } from "../System/SystemPanels";
import { MobileHistory } from "./MobileOrders";

function Stat({ label, value, cls, sub }: { label: string; value: string; cls?: string; sub?: string }) {
  return (
    <div className="flex flex-col gap-0.5 border border-bb-border bg-bb-panel px-3 py-2">
      <span className="text-[10px] tracking-widest text-bb-muted">{label}</span>
      <span data-numeric className={"text-[18px] " + (cls ?? "text-white")}>{value}</span>
      {sub && <span data-numeric className="text-[10px] text-bb-muted">{sub}</span>}
    </div>
  );
}

function Fold({ title, children, open: initial = false }: { title: string; children: React.ReactNode; open?: boolean }) {
  const [open, setOpen] = useState(initial);
  return (
    <div className="border-t border-bb-border">
      <button className="flex h-12 w-full items-center justify-between px-3 text-[12px] tracking-widest text-bb-amber" onClick={() => setOpen(!open)}>
        {title}
        <span className="text-bb-muted">{open ? "▴" : "▾"}</span>
      </button>
      {open && children}
    </div>
  );
}

/** Touch stepper over one risk setting. */
function RiskRow({ label, hint, value, unit, step, min, max, onChange, scale = 1 }: {
  label: string; hint?: string; value: number; unit: string; step: number; min: number; max: number;
  onChange: (v: number) => void; scale?: number;
}) {
  const shown = Math.round(value * scale * 100) / 100;
  const set = (v: number) => onChange(Math.min(max, Math.max(min, Math.round(v * 100) / 100)) / scale);
  return (
    <div className="flex items-center justify-between gap-2 py-1">
      <span className="flex min-w-0 flex-col">
        <span className="text-[12px] text-bb-muted">{label}</span>
        {hint && <span className="text-[10px] text-bb-muted">{hint}</span>}
      </span>
      <span className="flex shrink-0 items-center gap-1">
        <button className="h-10 w-10 border border-bb-border text-[16px] text-bb-muted active:bg-bb-amber active:text-black" onClick={() => set(shown - step)}>−</button>
        <span data-numeric className="w-16 text-center text-[15px] text-bb-amber">{shown}{unit}</span>
        <button className="h-10 w-10 border border-bb-border text-[16px] text-bb-muted active:bg-bb-amber active:text-black" onClick={() => set(shown + step)}>+</button>
      </span>
    </div>
  );
}

function RiskRules() {
  const account = useAccountStore((s) => s.account);
  const refreshAccount = useAccountStore((s) => s.refreshAccount);
  const [draft, setDraft] = useState<Partial<RiskSettings>>({});
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  if (!account) return null;
  const risk = { ...account.risk, ...draft };
  const dirty = Object.keys(draft).length > 0;
  const field = (k: keyof RiskSettings) => (v: number) => { setMsg(null); setDraft({ ...draft, [k]: v }); };
  return (
    <div className="px-3 pb-3">
      <div className="py-2"><RiskPresetChips touch /></div>
      <div className="flex items-center justify-between gap-2 py-1">
        <span className="flex min-w-0 flex-col">
          <span className="text-[12px] text-bb-muted">WORK SPREAD</span>
          <span className="text-[10px] text-bb-muted">entries/exits worked inside the bid-ask every {risk.spread_opt_step_s}s</span>
        </span>
        <button
          className={"h-10 shrink-0 border px-4 text-[12px] tracking-widest " + (risk.spread_optimizer ? "border-bb-amber bg-bb-amber font-semibold text-black" : "border-bb-border text-bb-muted")}
          onClick={() => { setMsg(null); setDraft({ ...draft, spread_optimizer: !risk.spread_optimizer }); }}
        >
          {risk.spread_optimizer ? "ON" : "OFF"}
        </button>
      </div>
      <RiskRow label="REPRICE EVERY" hint="spread optimizer rung cadence" value={risk.spread_opt_step_s} unit="s" step={0.5} min={0.5} max={30} onChange={field("spread_opt_step_s")} />
      <RiskRow label="TAKE PROFIT DEFAULT" value={risk.default_tp_pct} scale={100} unit="%" step={5} min={5} max={1000} onChange={field("default_tp_pct")} />
      <RiskRow label="STOP LOSS DEFAULT" value={risk.default_sl_pct} scale={100} unit="%" step={5} min={5} max={95} onChange={field("default_sl_pct")} />
      <RiskRow label="ENTRY TTL" hint="unfilled entries cancel after" value={risk.entry_ttl_min} unit="m" step={1} min={1} max={120} onChange={field("entry_ttl_min")} />
      <RiskRow label="MAX LOSS / TRADE" hint={`$${(account.equity * risk.max_loss_pct).toFixed(0)} at current equity`} value={risk.max_loss_pct} scale={100} unit="%" step={0.25} min={0.1} max={10} onChange={field("max_loss_pct")} />
      <RiskRow label="DAILY LOSS BREAKER" hint={`$${(account.equity * risk.daily_loss_pct).toFixed(0)} — halts new entries`} value={risk.daily_loss_pct} scale={100} unit="%" step={0.5} min={0.5} max={25} onChange={field("daily_loss_pct")} />
      <RiskRow label="MAX POSITIONS" value={risk.max_positions} unit="" step={1} min={1} max={20} onChange={field("max_positions")} />
      <RiskRow label="MAX TRADES / DAY" value={risk.max_trades_per_day} unit="" step={1} min={1} max={200} onChange={field("max_trades_per_day")} />
      <RiskRow label="PER-NAME NOTIONAL" hint="one share position, % of equity" value={risk.equity_max_notional_per_name_pct} scale={100} unit="%" step={1} min={1} max={100} onChange={field("equity_max_notional_per_name_pct")} />
      <RiskRow label="GROSS SHARE EXPOSURE" hint="all open share plans, % of equity" value={risk.equity_gross_exposure_pct} scale={100} unit="%" step={5} min={5} max={200} onChange={field("equity_gross_exposure_pct")} />
      <div className="mt-2 flex items-center gap-2">
        <button
          className="h-11 flex-1 bg-bb-amber text-[12px] font-semibold tracking-widest text-black disabled:opacity-40"
          disabled={!dirty || saving}
          onClick={async () => {
            setSaving(true);
            try {
              await putRisk(draft);
              setDraft({});
              await refreshAccount();
              setMsg("saved");
            } catch (err) {
              setMsg(`✗ ${apiError(err)}`);
            } finally {
              setSaving(false);
            }
          }}
        >
          {saving ? "…" : "SAVE"}
        </button>
        {dirty && <button className="h-11 border border-bb-border px-3 text-[12px] text-bb-muted" onClick={() => setDraft({})}>RESET</button>}
      </div>
      {msg && <div className={"mt-1 text-[11px] " + (msg.startsWith("✗") ? "text-bb-loss" : "text-bb-profit")}>{msg}</div>}
    </div>
  );
}

export function MobileAccount() {
  const account = useAccountStore((s) => s.account);
  const capsLine = capabilitiesSummaryLine(useAccountStore((s) => s.account?.capabilities));
  const { live } = useTradingMode();

  return (
    <div className="flex shrink-0 flex-col">
      <div className="grid grid-cols-2 gap-px border-t border-bb-border p-px">
        <Stat label="STATUS" value={account?.status ?? "—"} sub={live ? "LIVE" : "PAPER"} />
        <Stat label="DAY TRADES" value={String(account?.daytrade_count ?? "—")} sub="5 sessions" />
      </div>


      <Fold title={"CAPABILITIES" + (capsLine ? ` · ${capsLine}` : "")} open>
        <CapabilitiesPanel touch />
      </Fold>
      <Fold title="RISK RULES">
        <RiskRules />
      </Fold>
      <Fold title={live ? "LIVE ACCOUNT" : "PAPER ACCOUNT"}>
        <AccountsPanel />
      </Fold>
      <Fold title="CLOSED">
        <div className="flex max-h-[60dvh] flex-col">
          <MobileHistory />
        </div>
      </Fold>
    </div>
  );
}
