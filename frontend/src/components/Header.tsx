import { useSystemState } from "./System/SystemPanels";
import { useUiStore, type View } from "../store/uiStore";

const TABS: View[] = ["fund", "account", "strategies", "market", "system"];

/**
 * Feed health, read from the BACKEND's view rather than from a browser
 * socket.
 *
 * This used to reflect the terminal's WebSocket connection. The ops console
 * has no socket — it polls — so that pill would have sat on "CONNECTING"
 * forever, which is worse than no pill at all: a permanently wrong light
 * teaches you to ignore the light. What matters here is whether the ENGINE
 * is receiving market data, which is what system state reports.
 */
function StatusPill() {
  const state = useSystemState();
  if (!state) {
    return <span className="text-bb-muted" title="reading system state…">…</span>;
  }
  const age = state.feed.stream_age_s;
  let label = "LIVE";
  let cls = "text-bb-profit";
  let title = "Engine is receiving real-time market data";
  if (!state.feed.configured) {
    label = "NO KEYS";
    cls = "text-bb-loss";
    title = "Alpaca keys missing — the engine cannot price anything";
  } else if (age == null) {
    label = "NO STREAM";
    cls = "text-bb-orange";
    title = "Keys are configured but no stream message has arrived yet";
  } else if (age > 30) {
    label = `STALE ${Math.floor(age)}s`;
    cls = "text-bb-orange";
    title =
      "No stream message for a while. Outside 08:00–17:00 ET on the free IEX " +
      "feed this is expected, not a fault.";
  }
  return (
    <span className={`${cls} font-semibold`} title={title}>
      {label}
    </span>
  );
}

export function Header() {
  const view = useUiStore((s) => s.view);
  const setView = useUiStore((s) => s.setView);

  return (
    <header className="panel flex h-9 shrink-0 items-center gap-6 px-3">
      <span className="tracking-widest text-bb-amber">PLANETARIA</span>
      <span className="text-[11px] tracking-widest text-bb-muted">OPS</span>
      <div className="ml-auto flex items-center gap-4">
        {TABS.map((v) => (
          <button
            key={v}
            onClick={() => setView(v)}
            className={
              "px-2 py-0.5 text-[11px] tracking-widest " +
              (view === v
                ? "bg-bb-amber font-semibold text-black"
                : "text-bb-muted hover:text-bb-amber")
            }
          >
            {v.toUpperCase()}
          </button>
        ))}
        <StatusPill />
        <span className="border border-bb-border px-2 py-0.5 text-bb-orange">PAPER</span>
      </div>
    </header>
  );
}
