import { useState } from "react";
import type { LabPlan, Sim, SimTrade } from "./api";

const money = (n: number | null | undefined) =>
  n == null ? "—" : `${n < 0 ? "-" : ""}$${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
const tone = (n: number | null | undefined) =>
  n == null ? "text-bb-muted" : n > 0 ? "text-bb-profit" : n < 0 ? "text-bb-loss" : "text-white";
const et = (ts: string | null) =>
  ts ? new Date(ts).toLocaleString("en-US", { timeZone: "America/New_York", hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";

function SimRows({ trades }: { trades: SimTrade[] }) {
  if (!trades.length) return <p className="p-3 text-bb-muted">no simulated positions yet.</p>;
  return (
    <table className="w-full text-[11px]">
      <thead className="sticky top-0 bg-bb-panel text-[10px] uppercase text-bb-muted">
        <tr>
          {["opened", "sym", "side", "qty", "entry", "exit / mark", "why", "p&l"].map((h) => (
            <th key={h} className="px-2 py-1 text-left font-normal">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="mono">
        {trades.map((t, i) => {
          const live = t.exit === null;
          const pnl = live ? t.unrealized : t.pnl;
          return (
            <tr key={`${t.symbol}-${t.opened_at}-${i}`} className="border-t border-bb-border/40">
              <td className="px-2 py-0.5 text-bb-muted">{et(t.opened_at)}</td>
              <td className="px-2 py-0.5 text-white">{t.symbol}</td>
              <td className={`px-2 py-0.5 ${t.side > 0 ? "text-bb-profit" : "text-bb-loss"}`}>
                {t.side > 0 ? "LONG" : "SHORT"}
              </td>
              <td className="px-2 py-0.5 text-right text-white">{t.qty}</td>
              <td className="px-2 py-0.5 text-right text-white">{t.entry.toFixed(2)}</td>
              <td className="px-2 py-0.5 text-right text-white">
                {(t.exit ?? t.mark)?.toFixed(2) ?? "—"}
              </td>
              <td className={`px-2 py-0.5 text-[10px] uppercase ${live ? "text-bb-orange" : "text-bb-muted"}`}>
                {t.exit_reason ?? "open"}
              </td>
              <td className={`px-2 py-0.5 text-right ${tone(pnl)}`}>{money(pnl)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function BrokerRows({ plans }: { plans: LabPlan[] }) {
  if (!plans.length)
    return (
      <p className="p-3 text-bb-muted">
        no broker plans yet — with the free IEX feed every after-hours entry dies at the 120s quote-freshness
        gate, so this stays empty until real-time SIP is live.
      </p>
    );
  return (
    <table className="w-full text-[11px]">
      <thead className="sticky top-0 bg-bb-panel text-[10px] uppercase text-bb-muted">
        <tr>
          {["created", "sym", "status", "qty", "limit", "fill", "exit", "why", "slippage", "p&l"].map((h) => (
            <th key={h} className="px-2 py-1 text-left font-normal">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="mono">
        {plans.map((p) => (
          <tr key={p.id} className="border-t border-bb-border/40">
            <td className="px-2 py-0.5 text-bb-muted">{et(p.created_at)}</td>
            <td className="px-2 py-0.5 text-white">{p.underlying}</td>
            <td className="px-2 py-0.5 text-[10px] uppercase text-bb-orange">{p.status}</td>
            <td className="px-2 py-0.5 text-right text-white">{p.qty}</td>
            <td className="px-2 py-0.5 text-right text-white">{p.entry_limit?.toFixed(2)}</td>
            <td className="px-2 py-0.5 text-right text-white">{p.fill_premium?.toFixed(2) ?? "—"}</td>
            <td className="px-2 py-0.5 text-right text-white">{p.exit_premium?.toFixed(2) ?? "—"}</td>
            <td className="px-2 py-0.5 text-[10px] uppercase text-bb-muted">{p.exit_reason ?? "—"}</td>
            <td className="px-2 py-0.5 text-right text-bb-muted">
              {p.exec_quality?.entry?.cost != null ? p.exec_quality.entry.cost.toFixed(3) : "—"}
            </td>
            <td className={`px-2 py-0.5 text-right ${tone(p.realized_pnl)}`}>{money(p.realized_pnl)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function TradesPanel({ sim, plans }: { sim: Sim | null; plans: LabPlan[] }) {
  const [tab, setTab] = useState<"vol" | "flat" | "broker">("vol");
  const policy = sim?.policies.find((p) => p.policy === tab);
  const tabs: { id: "vol" | "flat" | "broker"; label: string }[] = [
    { id: "vol", label: `sim · vol (${sim?.policies.find((p) => p.policy === "vol")?.trades ?? 0})` },
    { id: "flat", label: `sim · flat (${sim?.policies.find((p) => p.policy === "flat")?.trades ?? 0})` },
    { id: "broker", label: `broker (${plans.length})` },
  ];
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-1 border-b border-bb-border px-3 py-1.5">
        <span className="mr-2 text-[11px] uppercase tracking-widest text-bb-amber">Positions</span>
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`border px-2 py-0.5 text-[10px] uppercase ${
              tab === t.id ? "border-bb-amber text-bb-amber" : "border-bb-border text-bb-muted"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {tab === "broker" ? <BrokerRows plans={plans} /> : <SimRows trades={policy?.positions ?? []} />}
      </div>
    </div>
  );
}
