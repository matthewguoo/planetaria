import { useState } from "react";
import { bodyOf, kindOf, summaryOf, type Decision, type Instance } from "./api";

const KIND_TONE: Record<string, string> = {
  decision: "text-bb-profit",
  would_trade: "text-bb-profit",
  placed: "text-bb-profit",
  intent: "text-bb-neutral",
  veto: "text-bb-loss",
  rejected: "text-bb-loss",
  error: "text-bb-loss",
  no_trade: "text-bb-muted",
  queued: "text-bb-orange",
  watchlist: "text-bb-amber",
  reanchor: "text-bb-amber",
  skip: "text-bb-muted",
};

const DIR_TONE: Record<string, string> = {
  bullish: "text-bb-profit",
  bearish: "text-bb-loss",
  neutral: "text-bb-muted",
};

const et = (ts: string) =>
  new Date(ts).toLocaleTimeString("en-US", {
    timeZone: "America/New_York",
    hour12: false,
  });

function Chip({ children, tone }: { children: React.ReactNode; tone?: string }) {
  return (
    <span className={`border border-bb-border px-1 text-[10px] uppercase ${tone ?? "text-bb-muted"}`}>
      {children}
    </span>
  );
}

function Detail({ decision }: { decision: Decision }) {
  const body = bodyOf(decision);
  const v = body?.verdict;
  return (
    <div className="border-l-2 border-bb-amber bg-bb-bg2 px-3 py-2 text-[11px]">
      {v?.summary && <p className="mb-2 text-white">{v.summary}</p>}
      {body && (
        <div className="mb-2 grid grid-cols-2 gap-x-6 gap-y-0.5 md:grid-cols-4">
          <Field k="px0 → px" v={`${body.px0?.toFixed(2)} → ${body.px?.toFixed(2)}`} />
          <Field k="tape move" v={fmtPct(body.move_pct)} />
          <Field k="day move" v={fmtPct(body.day_move_pct)} />
          <Field k="run 5d" v={fmtPct(body.run5d_pct)} />
          <Field k="spread" v={body.spread_pct == null ? "—" : `${body.spread_pct}%`} />
          <Field
            k="bracket"
            v={
              body.bracket
                ? `sl ${(body.bracket.sl_pct * 100).toFixed(1)}% / tp ${(body.bracket.tp_pct * 100).toFixed(1)}%`
                : "—"
            }
          />
          <Field k="eps / rev" v={`${v?.eps_vs_consensus ?? "—"} / ${v?.revenue_vs_consensus ?? "—"}`} />
          <Field k="guidance" v={v?.guidance ?? "—"} />
          {body.sizing && (
            <>
              <Field k="notional" v={`$${Math.round(body.sizing.notional).toLocaleString()}`} />
              <Field k="risk $" v={`$${Math.round(body.sizing.risk_dollars).toLocaleString()}`} />
              <Field
                k="multipliers"
                v={`conf ${body.sizing.conf_mult}× · flag ${body.sizing.flag_mult}× · move ${body.sizing.move_mult}×`}
              />
            </>
          )}
          <Field k="model" v={`${body.model ?? "—"} ${body.latency_ms ? `(${(body.latency_ms / 1000).toFixed(1)}s)` : ""}`} />
        </div>
      )}
      {!!v?.quality_flags?.length && (
        <div className="mb-2 flex flex-wrap gap-1">
          {v.quality_flags.map((f) => (
            <Chip key={f} tone="text-bb-orange">
              {f.replace(/_/g, " ")}
            </Chip>
          ))}
        </div>
      )}
      <details>
        <summary className="cursor-pointer text-bb-muted">raw journal row</summary>
        <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap text-[10px] text-bb-muted">
          {JSON.stringify(decision.detail, null, 2)}
        </pre>
      </details>
    </div>
  );
}

const fmtPct = (n?: number | null) => (n == null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(2)}%`);

function Field({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-bb-muted">{k}</span>
      <span className="mono text-white">{v}</span>
    </div>
  );
}

export default function DecisionTape({
  decisions,
  instances,
}: {
  decisions: Decision[];
  instances: Instance[];
}) {
  const [open, setOpen] = useState<number | null>(null);
  const [filter, setFilter] = useState<"all" | "acted">("all");
  const names = Object.fromEntries(instances.map((i) => [i.id, i.name]));

  const acted = new Set(["decision", "would_trade", "veto", "placed", "rejected", "error"]);
  const rows = decisions.filter((d) => filter === "all" || acted.has(kindOf(d)));

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-bb-border px-3 py-1.5">
        <span className="text-[11px] uppercase tracking-widest text-bb-amber">Decision tape</span>
        <div className="flex gap-1">
          {(["all", "acted"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`border px-2 py-0.5 text-[10px] uppercase ${
                filter === f ? "border-bb-amber text-bb-amber" : "border-bb-border text-bb-muted"
              }`}
            >
              {f}
            </button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {rows.length === 0 && (
          <p className="p-3 text-bb-muted">
            nothing journaled yet — the 15:30 ET tick freezes the watchlist and the tape starts there.
          </p>
        )}
        {rows.map((d) => {
          const kind = kindOf(d);
          const body = bodyOf(d);
          const dir = body?.verdict?.direction;
          return (
            <div key={d.id} className="border-b border-bb-border/50">
              <button
                onClick={() => setOpen(open === d.id ? null : d.id)}
                className="flex w-full items-center gap-2 px-3 py-1 text-left hover:bg-bb-hover"
              >
                <span className="mono w-16 shrink-0 text-[11px] text-bb-muted">{et(d.ts)}</span>
                <span className={`w-24 shrink-0 text-[10px] uppercase ${KIND_TONE[kind] ?? "text-bb-muted"}`}>
                  {kind.replace(/_/g, " ")}
                </span>
                <span className="mono w-14 shrink-0 text-white">{body?.symbol ?? ""}</span>
                {dir && <Chip tone={DIR_TONE[dir]}>{dir}</Chip>}
                {body?.verdict?.confidence && <Chip>{body.verdict.confidence}</Chip>}
                {body?.move_pct != null && (
                  <span
                    className={`mono w-16 text-right ${body.move_pct >= 0 ? "text-bb-profit" : "text-bb-loss"}`}
                  >
                    {fmtPct(body.move_pct)}
                  </span>
                )}
                <span className="w-20 shrink-0 truncate text-[10px] text-bb-muted">
                  {names[d.strategy_id] ?? ""}
                </span>
                <span className="truncate text-[11px] text-bb-muted">{summaryOf(d)}</span>
              </button>
              {open === d.id && <Detail decision={d} />}
            </div>
          );
        })}
      </div>
    </div>
  );
}
