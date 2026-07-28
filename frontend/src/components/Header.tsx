import { useTradingStore } from "../store/tradingStore";
import { useUiStore } from "../store/uiStore";

function StatusPill() {
  const status = useTradingStore((s) => s.status);
  const symbol = useTradingStore((s) => s.symbol);
  let label = "LIVE";
  let cls = "text-bb-profit";
  let title = "Real-time Alpaca market data";
  if (status.connection !== "open") {
    label = status.connection === "connecting" ? "CONNECTING" : "DISCONNECTED";
    cls = "text-bb-loss";
    title = "Feed socket is not connected";
  } else if (status.demo) {
    // Keyless mode: real public prices when the public feed is reachable,
    // synthetic random walk only as a last resort. Never tradeable.
    if (status.sources[symbol] === "public") {
      label = "PUBLIC DATA";
      cls = "text-bb-amber";
      title =
        "Real prices from a keyless public feed (Yahoo, ~5s poll). " +
        "Options chain is modeled from this spot. Set Alpaca keys in .env for tradeable data.";
    } else {
      label = "DEMO DATA";
      cls = "text-bb-neutral";
      title =
        "Synthetic random-walk prices — public feed unreachable and no Alpaca keys. " +
        "NOT real market data.";
    }
  } else if (!status.configured) {
    label = "NO KEYS";
    cls = "text-bb-loss";
    title = "Alpaca keys missing";
  } else if (status.stream_age_s !== null && status.stream_age_s > 30) {
    label = `STALE ${Math.floor(status.stream_age_s)}s`;
    cls = "text-bb-orange";
    title = "No stream message for a while — quotes may be stale";
  }
  return (
    <span className={`${cls} font-semibold`} title={title}>
      {label}
    </span>
  );
}

export function Header() {
  const symbol = useTradingStore((s) => s.symbol);
  const quote = useTradingStore((s) => s.quote);
  const view = useUiStore((s) => s.view);
  const setView = useUiStore((s) => s.setView);

  return (
    <header className="panel flex h-9 shrink-0 items-center gap-6 px-3">
      <span className="tracking-widest text-bb-amber">PLANETARIA</span>
      <span className="text-white">{symbol}</span>
      {quote ? (
        <span data-numeric className="text-bb-amber">
          {quote.mid ? quote.mid.toFixed(2) : "—"}
          <span className="ml-3 text-bb-muted">
            {quote.bid.toFixed(2)} × {quote.ask.toFixed(2)}
          </span>
        </span>
      ) : (
        <span className="text-bb-muted">no quote</span>
      )}
      <div className="ml-auto flex items-center gap-4">
        {(["terminal", "account"] as const).map((v) => (
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
