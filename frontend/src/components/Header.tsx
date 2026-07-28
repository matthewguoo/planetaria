import { useTradingStore } from "../store/tradingStore";

function StatusPill() {
  const status = useTradingStore((s) => s.status);
  let label = "LIVE";
  let cls = "text-bb-profit";
  if (status.connection !== "open") {
    label = status.connection === "connecting" ? "CONNECTING" : "DISCONNECTED";
    cls = "text-bb-loss";
  } else if (status.demo) {
    label = "DEMO DATA";
    cls = "text-bb-neutral";
  } else if (!status.configured) {
    label = "NO KEYS";
    cls = "text-bb-loss";
  } else if (status.stream_age_s !== null && status.stream_age_s > 30) {
    label = `STALE ${Math.floor(status.stream_age_s)}s`;
    cls = "text-bb-orange";
  }
  return <span className={`${cls} font-semibold`}>{label}</span>;
}

export function Header() {
  const symbol = useTradingStore((s) => s.symbol);
  const quote = useTradingStore((s) => s.quote);

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
        <StatusPill />
        <span className="border border-bb-border px-2 py-0.5 text-bb-orange">PAPER</span>
      </div>
    </header>
  );
}
