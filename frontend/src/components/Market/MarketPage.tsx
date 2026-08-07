/**
 * MARKET — the overview a strategy operator wants before the bell and after
 * the close: is the market open, where are the majors, what is the engine
 * actually going to see tonight, and what has it seen so far.
 *
 * All of it comes from endpoints that already existed. The earnings calendar
 * and the news tape are the same `estimate` and `news` signal rows the
 * strategies consume, which is the point — this page shows the engine's own
 * view of the market rather than a second, prettier one that could disagree
 * with it.
 */

import { useEffect, useState } from "react";
import { getQuote, getSignals, type Quote, type SignalRecord } from "../../lib/api";
import { useSystemState } from "../System/SystemPanels";

// Each quote is a broker REST call and shows up in SYSTEM's call flow, so
// this is deliberately slow — the majors are context, not a trading signal.
const QUOTE_POLL_MS = 20_000;
const SIGNAL_POLL_MS = 10_000;
const MAJORS = ["SPY", "QQQ", "IWM", "DIA"];

function ET(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function MajorTile({ symbol }: { symbol: string }) {
  const [quote, setQuote] = useState<Quote | null>(null);
  const [dead, setDead] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      getQuote(symbol)
        .then((q) => {
          if (!alive) return;
          setQuote(q);
          setDead(false);
        })
        .catch(() => alive && setDead(true));
    void load();
    const id = window.setInterval(load, QUOTE_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [symbol]);

  const mid = quote?.mid ?? null;
  const spread =
    quote?.bid && quote?.ask && quote.ask > quote.bid ? quote.ask - quote.bid : null;
  // A quote's age is the whole story after hours: the free feed goes quiet at
  // 17:00 ET and keeps serving the last print for hours.
  const ageS = quote?.ts ? (Date.now() - quote.ts) / 1000 : null;

  return (
    <div className="panel flex flex-col justify-between px-3 py-2">
      <div className="flex items-baseline justify-between">
        <span className="text-[11px] tracking-widest text-bb-amber">{symbol}</span>
        {ageS != null && (
          <span
            className={"text-[9px] " + (ageS > 300 ? "text-bb-orange" : "text-bb-muted")}
            data-numeric
            title="Age of the last quote. The free feed stops updating outside 08:00–17:00 ET."
          >
            {ageS < 90 ? `${ageS.toFixed(0)}s` : `${(ageS / 60).toFixed(0)}m`}
          </span>
        )}
      </div>
      <div className="text-[18px] text-white" data-numeric>
        {dead ? <span className="text-[12px] text-bb-muted">no data</span>
          : mid != null ? mid.toFixed(2) : "—"}
      </div>
      <div className="text-[10px] text-bb-muted" data-numeric>
        {quote?.bid != null && quote?.ask != null
          ? `${quote.bid.toFixed(2)} / ${quote.ask.toFixed(2)}${
              spread != null && mid ? ` · ${((spread / mid) * 1e4).toFixed(0)}bp` : ""
            }`
          : "—"}
      </div>
    </div>
  );
}

function useSignals(type: string, limit: number): SignalRecord[] {
  const [rows, setRows] = useState<SignalRecord[]>([]);
  useEffect(() => {
    let alive = true;
    const load = () =>
      getSignals({ type, limit })
        .then((s) => alive && setRows(s))
        .catch(() => {});
    void load();
    const id = window.setInterval(load, SIGNAL_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [type, limit]);
  return rows;
}

function ClockBar() {
  const state = useSystemState();
  const clock = state?.broker.market_clock;
  // `known: false` is not "closed" — it is "the broker calendar has not been
  // fetched yet". Saying CLOSED there would be a guess presented as a fact.
  const label = !clock?.known ? "UNKNOWN" : clock.is_open ? "OPEN" : "CLOSED";
  const cls = !clock?.known
    ? "text-bb-muted"
    : clock.is_open
      ? "text-bb-profit"
      : "text-bb-orange";

  return (
    <div className="panel flex shrink-0 flex-wrap items-center gap-x-6 gap-y-1 px-3 py-2 text-[11px]">
      <span className="tracking-widest text-bb-muted">US EQUITIES</span>
      <span className={`font-semibold ${cls}`}>{label}</span>
      <span className="text-bb-muted">
        next open <span className="text-white" data-numeric>{ET(clock?.next_open)}</span>
      </span>
      <span className="text-bb-muted">
        next close <span className="text-white" data-numeric>{ET(clock?.next_close)}</span>
      </span>
      <span className="ml-auto text-bb-muted">
        streaming{" "}
        <span className="text-bb-amber">
          {state?.feed.stock_symbols.length ? state.feed.stock_symbols.join(" ") : "—"}
        </span>
      </span>
    </div>
  );
}

function EarningsPanel() {
  const rows = useSignals("estimate", 60);
  // One estimate row per symbol per date; the calendar feed republishes on
  // every fetch, so the raw list is mostly duplicates.
  const seen = new Set<string>();
  const unique = rows.filter((r) => {
    const p = r.payload ?? {};
    const key = `${String(p.symbol)}:${String(p.date)}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  return (
    <div className="panel flex min-h-0 flex-col">
      <div className="panel-title" title="the `estimate` signals the earnings strategies watch">
        EARNINGS CALENDAR ({unique.length})
      </div>
      {unique.length === 0 ? (
        <div className="flex h-16 items-center justify-center px-3 text-center text-[11px] text-bb-muted">
          no estimates journalled — the calendar feed needs FINNHUB_API_KEY in .env
        </div>
      ) : (
        <div className="max-h-72 overflow-y-auto">
          <table className="w-full border-collapse text-[11px]">
            <thead className="text-[10px] text-bb-muted">
              <tr className="border-b border-bb-border">
                <th className="px-2 py-1 text-left">DATE</th>
                <th className="px-2 py-1 text-left">SYMBOL</th>
                <th className="px-2 py-1 text-left">WHEN</th>
                <th className="px-2 py-1 text-right">EPS CONS.</th>
                <th className="px-2 py-1 text-right">REV CONS.</th>
              </tr>
            </thead>
            <tbody>
              {unique.map((r) => {
                const p = r.payload ?? {};
                const when = String(p.when ?? "unknown");
                const rev = p.revenue_consensus;
                return (
                  <tr key={r.id} className="border-b border-bb-border/50 hover:bg-bb-hover">
                    <td className="px-2 py-1 text-bb-muted" data-numeric>{String(p.date ?? "—")}</td>
                    <td className="px-2 py-1 text-bb-amber">{String(p.symbol ?? "—")}</td>
                    <td
                      className={"px-2 py-1 " + (when === "amc" ? "text-bb-profit" : "text-bb-muted")}
                      title={when === "amc" ? "after market close — what the earnings strategies trade" : ""}
                    >
                      {when}
                    </td>
                    <td className="px-2 py-1 text-right text-bb-neutral" data-numeric>
                      {p.eps_consensus == null ? "—" : String(p.eps_consensus)}
                    </td>
                    <td className="px-2 py-1 text-right text-bb-neutral" data-numeric>
                      {rev == null ? "—" : `${(Number(rev) / 1e6).toFixed(0)}M`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function NewsPanel() {
  const rows = useSignals("news", 40);
  return (
    <div className="panel flex min-h-0 flex-col">
      <div className="panel-title" title="the `news` signals the strategies react to (Benzinga + EDGAR 8-K)">
        TAPE ({rows.length})
      </div>
      {rows.length === 0 ? (
        <div className="flex h-16 items-center justify-center text-[11px] text-bb-muted">
          no news journalled yet
        </div>
      ) : (
        <div className="max-h-72 overflow-y-auto">
          <table className="w-full border-collapse text-[11px]">
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="border-b border-bb-border/50 hover:bg-bb-hover">
                  <td className="whitespace-nowrap px-2 py-1 align-top text-bb-muted" data-numeric>
                    {r.ts?.slice(11, 19) ?? "—"}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1 align-top text-bb-amber">
                    {(r.symbols ?? []).join(" ") || "—"}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1 align-top text-[10px] text-bb-muted">
                    {r.source}
                  </td>
                  <td
                    className="max-w-0 truncate px-2 py-1 align-top text-bb-neutral"
                    title={String((r.payload ?? {}).headline ?? "")}
                  >
                    {String((r.payload ?? {}).headline ?? (r.payload ?? {}).text ?? "—")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function MarketPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-px overflow-y-auto">
      <ClockBar />
      <div className="grid shrink-0 grid-cols-2 gap-px md:grid-cols-4">
        {MAJORS.map((s) => (
          <MajorTile key={s} symbol={s} />
        ))}
      </div>
      <EarningsPanel />
      <NewsPanel />
    </div>
  );
}
