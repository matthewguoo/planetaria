/**
 * ACCOUNT on a phone: the numbers as a stat grid, the equity curve, then
 * the persistent settings — capabilities (what this account may trade),
 * the risk rules the server enforces, which broker account is selected —
 * and the closed-trade history. Every control is finger-sized; every
 * change saves on its own button.
 */

import { useCallback, useState } from "react";
import { apiError, getAccountHistory, putRisk, type PortfolioHistory, type RiskSettings } from "../../lib/api";
import { usePoll } from "../../lib/usePoll";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
import { capabilitiesSummaryLine } from "../../lib/capabilities";
import { EquityCurve } from "../Account/AccountPage";
import { CapabilitiesPanel } from "../Account/CapabilitiesPanel";
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
              setMsg("saved — enforced from the next entry");
            } catch (err) {
              setMsg(`✗ ${apiError(err)}`);
            } finally {
              setSaving(false);
            }
          }}
        >
          {saving ? "SAVING…" : "SAVE RULES"}
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
  const [history, setHistory] = useState<PortfolioHistory | null>(null);
  const [period, setPeriod] = useState("1M");

  const refresh = useCallback(async (alive: () => boolean) => {
    try {
      const h = await getAccountHistory(period, period === "1D" ? "15Min" : period === "1W" ? "1H" : "1D");
      if (alive()) setHistory(h);
    } catch {
      if (alive()) setHistory({ timestamps: [], equity: [], profit_loss: [], base_value: null });
    }
  }, [period]);
  usePoll(refresh, 20_000, [refresh]);

  return (
    <div className="flex shrink-0 flex-col">
      <div className="grid grid-cols-2 gap-px border-t border-bb-border p-px">
        <Stat label="STATUS" value={account?.status ?? "—"} sub={live ? "LIVE · real money" : "PAPER"} />
        <Stat label="DAY TRADES" value={String(account?.daytrade_count ?? "—")} sub="rolling 5 sessions" />
      </div>

      <div className="border-t border-bb-border">
        <div className="flex h-10 items-center justify-between px-3">
          <span className="text-[10px] tracking-widest text-bb-muted">EQUITY CURVE</span>
          <span className="flex gap-px">
            {["1D", "1W", "1M", "3M", "1A"].map((p) => (
              <button key={p} onClick={() => setPeriod(p)} className={"h-8 px-2 text-[11px] " + (period === p ? "bg-bb-amber font-semibold text-black" : "text-bb-muted")}>
                {p}
              </button>
            ))}
          </span>
        </div>
        <div className="h-40 px-1 pb-1">
          <EquityCurve history={history} />
        </div>
      </div>

      <Fold title={"CAPABILITIES" + (capsLine ? ` · ${capsLine}` : "")} open>
        <CapabilitiesPanel touch />
      </Fold>
      <Fold title="RISK RULES (SERVER-ENFORCED)">
        <RiskRules />
      </Fold>
      <Fold title={live ? "LIVE ACCOUNT" : "PAPER ACCOUNT"}>
        <AccountsPanel />
      </Fold>
      <Fold title="CLOSED TRADES">
        <div className="flex max-h-[60dvh] flex-col">
          <MobileHistory />
        </div>
      </Fold>
    </div>
  );
}
