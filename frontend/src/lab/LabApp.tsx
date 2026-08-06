import { useCallback, useEffect, useRef, useState } from "react";
import CallsPanel from "./CallsPanel";
import DecisionTape from "./DecisionTape";
import EquityCurves from "./EquityCurves";
import TradesPanel from "./TradesPanel";
import { fetchSim, fetchStream, type Analysis, type Decision, type Sim, type Stream } from "./api";

const POLL_MS = 2_000;
const SIM_EVERY = 3; // sim is a full replay — a third of the tape cadence is plenty
const MAX_ROWS = 1_000;

/** Merge newest-first pages, newest first, de-duped by id. */
function merge<T extends { id: number }>(prev: T[], next: T[]): T[] {
  if (!next.length) return prev;
  const seen = new Set(next.map((r) => r.id));
  return [...next, ...prev.filter((r) => !seen.has(r.id))].slice(0, MAX_ROWS);
}

/** Proof the page is alive when the data is not. An idle engine emits
 * nothing for hours; without this, "no new rows" and "the tab is frozen"
 * look identical — which is exactly how it read at 01:20 ET. */
function Heartbeat({
  beat,
  error,
}: {
  beat: { at: number; polls: number; lastNew: number; fresh: number };
  error: string | null;
}) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, []);
  const since = beat.at ? Math.round((now - beat.at) / 1000) : null;
  const stalled = since !== null && since > 10;
  const tone = error || stalled ? "text-bb-loss" : "text-bb-profit";
  const ago = (t: number) => {
    const s = Math.round((now - t) / 1000);
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    return `${Math.round(s / 3600)}h`;
  };
  return (
    <span className="flex items-baseline gap-1.5 border border-bb-border px-2 py-0.5">
      <span
        className={`inline-block h-1.5 w-1.5 self-center ${tone === "text-bb-profit" ? "bg-bb-profit" : "bg-bb-loss"}`}
        style={{ opacity: beat.polls % 2 === 0 ? 1 : 0.25 }}
      />
      <span className="text-[10px] uppercase text-bb-muted">polled</span>
      <span className={`mono text-[11px] ${tone}`}>
        {error ? "ERROR" : since === null ? "…" : `${since}s ago · ${beat.polls}`}
      </span>
      <span className="text-[10px] uppercase text-bb-muted">last new</span>
      <span className="mono text-[11px] text-white">
        {beat.lastNew ? ago(beat.lastNew) : "—"}
      </span>
    </span>
  );
}

function Badge({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <span className="flex items-baseline gap-1.5 border border-bb-border px-2 py-0.5">
      <span className="text-[10px] uppercase text-bb-muted">{label}</span>
      <span className={`mono text-[11px] ${tone ?? "text-white"}`}>{value}</span>
    </span>
  );
}

export default function LabApp() {
  const [stream, setStream] = useState<Stream | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [analyses, setAnalyses] = useState<Analysis[]>([]);
  const [sim, setSim] = useState<Sim | null>(null);
  const [equity, setEquity] = useState(100_000);
  const [flatPct, setFlatPct] = useState(10);
  const [error, setError] = useState<string | null>(null);
  // Liveness, not decoration: an idle overnight engine produces no new rows
  // for hours, and without a heartbeat that is indistinguishable from a
  // frozen page. polls counts round trips; lastNew is when data last moved.
  const [beat, setBeat] = useState({ at: 0, polls: 0, lastNew: 0, fresh: 0 });
  const cursor = useRef({ decision: 0, signal: 0 });
  const tick = useRef(0);

  const poll = useCallback(async () => {
    try {
      const data = await fetchStream(cursor.current);
      cursor.current = data.cursor;
      setStream(data);
      setDecisions((prev) => merge(prev, data.decisions));
      setAnalyses((prev) => merge(prev, data.analyses));
      setError(null);
      const fresh = data.decisions.length + data.analyses.length;
      setBeat((b) => ({
        at: Date.now(),
        polls: b.polls + 1,
        lastNew: fresh ? Date.now() : b.lastNew,
        fresh,
      }));
      if (tick.current % SIM_EVERY === 0) setSim(await fetchSim(equity, flatPct));
      tick.current += 1;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [equity, flatPct]);

  useEffect(() => {
    void poll();
    const id = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(id);
  }, [poll]);

  // Re-run the twin immediately when its inputs change — waiting for the
  // next scheduled sim tick makes the controls feel broken.
  useEffect(() => {
    fetchSim(equity, flatPct).then(setSim).catch(() => undefined);
  }, [equity, flatPct]);

  const live = stream?.instances.filter((i) => i.params.live === true) ?? [];
  const shadow = stream?.instances.filter((i) => i.params.live !== true) ?? [];
  const sipOn = stream?.feed.stock === "sip";

  return (
    <div className="flex h-full flex-col bg-black text-bb-amber">
      <header className="flex flex-wrap items-center gap-2 border-b border-bb-border bg-bb-panel px-3 py-1.5">
        <span className="text-[13px] font-bold tracking-[0.2em] text-bb-amber">PLANETARIA · LAB</span>
        <span className="text-[10px] uppercase text-bb-muted">LLM-gated earnings reaction</span>
        <div className="ml-auto flex flex-wrap items-center gap-1.5">
          <Badge
            label="feed"
            value={stream?.feed.stock?.toUpperCase() ?? "—"}
            tone={sipOn ? "text-bb-profit" : "text-bb-loss"}
          />
          <Badge
            label="llm"
            value={`${stream?.llm.model ?? "—"} · ${stream?.llm.backend ?? "—"}`}
            tone={stream?.llm.configured ? "text-bb-profit" : "text-bb-loss"}
          />
          <Badge
            label="live"
            value={live.map((i) => i.name).join(", ") || "none"}
            tone={live.length ? "text-bb-profit" : "text-bb-muted"}
          />
          <Badge label="shadow" value={shadow.map((i) => i.name).join(", ") || "none"} />
          {stream?.instances.map((i) => (
            <Badge
              key={i.id}
              label={i.name}
              value={`${i.state} · ${i.runtime?.task ?? "?"} · q${i.runtime?.queue_size ?? 0} · e${
                i.runtime?.errors ?? 0
              }`}
              tone={
                i.state === "enabled" && i.runtime?.task === "running"
                  ? "text-bb-profit"
                  : "text-bb-orange"
              }
            />
          ))}
          <Heartbeat beat={beat} error={error} />
        </div>
      </header>

      {!sipOn && (
        <div className="border-b border-bb-loss bg-bb-loss/10 px-3 py-1 text-[11px] text-bb-loss">
          Stock feed is IEX. After-hours quotes go stale at 16:00 ET, so every real AH entry will be refused by
          the 120s freshness gate — the twin below still scores the decisions, but the broker column will stay
          empty until the SIP subscription is on and the feed is flipped.
        </div>
      )}
      {error && (
        <div className="border-b border-bb-loss bg-bb-loss/10 px-3 py-1 text-[11px] text-bb-loss">{error}</div>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-px bg-bb-border lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
        <div className="flex min-h-0 flex-col gap-px bg-bb-border">
          <div className="min-h-0 flex-[3] bg-bb-panel">
            <DecisionTape decisions={decisions} instances={stream?.instances ?? []} />
          </div>
          <div className="min-h-0 flex-[2] bg-bb-panel">
            <CallsPanel analyses={analyses} />
          </div>
        </div>

        <div className="flex min-h-0 flex-col gap-px bg-bb-border">
          <div className="bg-bb-panel">
            <EquityCurves sim={sim} />
            <div className="flex flex-wrap items-center gap-3 border-t border-bb-border px-3 py-1.5 text-[11px]">
              <label className="flex items-center gap-1.5">
                <span className="text-[10px] uppercase text-bb-muted">start equity</span>
                <input
                  type="number"
                  step={10_000}
                  min={1_000}
                  value={equity}
                  onChange={(e) => setEquity(Math.max(1_000, Number(e.target.value) || 0))}
                  className="mono w-28 border border-bb-border bg-black px-1 text-white"
                />
              </label>
              <label className="flex items-center gap-1.5">
                <span className="text-[10px] uppercase text-bb-muted">flat alloc %</span>
                <input
                  type="number"
                  step={1}
                  min={1}
                  max={100}
                  value={flatPct}
                  onChange={(e) => setFlatPct(Math.min(100, Math.max(1, Number(e.target.value) || 1)))}
                  className="mono w-16 border border-bb-border bg-black px-1 text-white"
                />
              </label>
              <span className="text-bb-muted">
                both allocators take identical entries, brackets and exits — the gap is sizing alone
              </span>
            </div>
          </div>
          <div className="min-h-0 flex-1 bg-bb-panel">
            <TradesPanel sim={sim} plans={stream?.plans ?? []} />
          </div>
        </div>
      </div>
    </div>
  );
}
