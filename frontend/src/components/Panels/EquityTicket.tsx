/**
 * Manual EQUITY / ETF swing ticket. The discipline is structural: sizing is
 * risk-%-of-the-manual-book against the stop distance, the stop is REQUIRED
 * (the backend refuses a stopless manual equity entry), the target is
 * optional (let winners run), and every gate shows its refusal reason
 * BEFORE submit. The server re-validates everything.
 */

import { useState } from "react";
import { apiError, postOrder } from "../../lib/api";
import { playCue } from "../../lib/audio";
import {
  capitalUsd,
  equityExits,
  equityPreflight,
  rr,
  sharesForRisk,
  swingBackstopUtc,
} from "../../lib/equityMath";
import { useAccountStore } from "../../store/accountStore";
import { freshSpot, quoteIsStale, useTradingStore } from "../../store/tradingStore";

/** ET 15:55 on a calendar date -> UTC ISO (same offset trick as OrderPanel). */
function etCloseToUtcIso(dateStr: string): string {
  const now = new Date();
  const etNow = new Date(now.toLocaleString("en-US", { timeZone: "America/New_York" }));
  const offsetMs = now.getTime() - etNow.getTime();
  const [y, m, d] = dateStr.split("-").map(Number);
  const etTarget = new Date(etNow);
  etTarget.setFullYear(y, m - 1, d);
  etTarget.setHours(15, 55, 0, 0);
  return new Date(etTarget.getTime() + offsetMs).toISOString();
}

function Row({ label, value, cls, title }: { label: string; value: string; cls?: string; title?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2" title={title}>
      <span className="text-[10px] text-bb-muted">{label}</span>
      <span data-numeric className={"text-[11px] " + (cls ?? "text-white")}>{value}</span>
    </div>
  );
}

function PctField({
  label, value, onChange, step = 0.5, min = 0.1, max = 100, accent,
}: {
  label: string; value: number; onChange: (v: number) => void;
  step?: number; min?: number; max?: number; accent?: string;
}) {
  return (
    <label className="flex items-center justify-between gap-2">
      <span className="text-[10px] text-bb-muted">{label}</span>
      <span className="inline-flex items-center gap-1">
        <input
          data-numeric
          type="number"
          step={step}
          min={min}
          max={max}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          className={
            "w-16 border border-bb-border bg-black px-1 py-0.5 text-right text-[11px] outline-none focus:border-bb-amber " +
            (accent ?? "text-white")
          }
        />
        <span className="text-[10px] text-bb-muted">%</span>
      </span>
    </label>
  );
}

export function EquityTicket() {
  const symbol = useTradingStore((s) => s.symbol);
  const quote = useTradingStore((s) => s.quote);
  const account = useAccountStore((s) => s.account);
  const refreshAccount = useAccountStore((s) => s.refreshAccount);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);

  const [side, setSide] = useState<1 | -1>(1);
  const [riskPct, setRiskPct] = useState(1.0);
  const [slPct, setSlPct] = useState(5.0);
  const [tpOn, setTpOn] = useState(false);
  const [tpPct, setTpPct] = useState(10.0);
  const [sharesOverride, setSharesOverride] = useState(0); // 0 = auto
  const [timeStopDate, setTimeStopDate] = useState(""); // "" = +30d backstop
  const [extendedHours, setExtendedHours] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [placed, setPlaced] = useState<string | null>(null);

  const book = account?.manual_book;
  const quoteFresh = !!quote && quote.mid > 0 && !quoteIsStale(quote);
  // Marketable pricing: pay the ask long, hit the bid short; mid fallback.
  const rawPx = side > 0 ? quote?.ask : quote?.bid;
  const price = quoteFresh && rawPx && rawPx > 0 ? rawPx : freshSpot(quote, 0);
  const halfSpread =
    quote && quote.ask > 0 && quote.bid > 0 && quote.ask >= quote.bid
      ? (quote.ask - quote.bid) / 2
      : null;

  const exits = equityExits(price, side, slPct / 100, tpOn ? tpPct / 100 : null);
  const bookEquity = book?.enabled ? book.equity_usd : account?.equity ?? 0;
  const riskBudget = (bookEquity * riskPct) / 100;
  const autoShares = price > 0 ? sharesForRisk(riskBudget, exits.entry, exits.sl) : 0;
  const shares = sharesOverride > 0 ? Math.min(sharesOverride, Math.max(autoShares, 1)) : autoShares;
  const notional = capitalUsd(price, shares, side);
  const maxLoss = (exits.entry - exits.sl) * shares;
  const rrMult = rr(exits.entry, exits.tp, exits.sl);
  const longOnly = account?.risk?.equity_long_only ?? true;

  const reasons = equityPreflight({
    price, side, shares, exits, book,
    equityLongOnly: longOnly,
    quoteFresh,
  });
  const canTrade = reasons.length === 0 && shares > 0 && !submitting;

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const plan = await postOrder({
        underlying: symbol,
        strategy: "manual_equity",
        asset_class: "equity",
        extended_hours: extendedHours,
        legs: [{
          symbol,
          side,
          ratio: 1,
          entry: Number(exits.entry.toFixed(2)),
          half_spread: halfSpread != null ? Number(halfSpread.toFixed(4)) : null,
        }],
        qty: shares,
        entry_limit: Number(exits.entry.toFixed(2)),
        tp_premium: exits.tp,
        sl_premium: exits.sl,
        time_stop_utc: timeStopDate ? etCloseToUtcIso(timeStopDate) : swingBackstopUtc(),
      });
      setPlaced(plan.id);
      setConfirming(false);
      void refreshPositions();
      void refreshAccount();
    } catch (err) {
      playCue("rejected");
      setError(apiError(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="panel relative flex min-w-0 flex-col">
      <div className="panel-title flex items-center justify-between">
        <span>EQUITY SWING TICKET</span>
        {book?.enabled && (
          <span className="text-[9px] tracking-normal text-bb-muted" title="Manual book envelope: used / total. Sized to mirror the real account.">
            BOOK ${Math.round(book.used_usd).toLocaleString()} / ${Math.round(book.equity_usd).toLocaleString()}
          </span>
        )}
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto p-2">
        <div className="flex gap-px">
          {([1, -1] as const).map((s) => (
            <button
              key={s}
              onClick={() => setSide(s)}
              className={
                "flex-1 py-1 text-[11px] tracking-widest " +
                (side === s
                  ? s > 0
                    ? "bg-bb-profit font-semibold text-black"
                    : "bg-bb-loss font-semibold text-black"
                  : "border border-bb-border text-bb-muted hover:text-bb-amber")
              }
            >
              {s > 0 ? "LONG" : "SHORT"}
            </button>
          ))}
        </div>
        {side < 0 && longOnly && (
          <div className="text-[10px] text-bb-orange">
            shorts are gated off (equity_long_only) — long-only book
          </div>
        )}

        <PctField label="RISK (% OF BOOK)" value={riskPct} onChange={setRiskPct} step={0.25} max={10} accent="text-bb-amber" />
        <PctField label="STOP DISTANCE" value={slPct} onChange={setSlPct} step={0.5} max={50} accent="text-bb-loss" />
        <label className="flex items-center justify-between gap-2">
          <span className="text-[10px] text-bb-muted">TARGET</span>
          <span className="inline-flex items-center gap-1">
            <button
              onClick={() => setTpOn(!tpOn)}
              className={
                "border px-1.5 py-0.5 text-[10px] " +
                (tpOn ? "border-bb-profit text-bb-profit" : "border-bb-border text-bb-muted")
              }
              title="Optional: no target = let the winner run under the hard stop"
            >
              {tpOn ? "ON" : "RUN"}
            </button>
            {tpOn && (
              <input
                data-numeric type="number" step={1} min={1} max={200} value={tpPct}
                onChange={(e) => setTpPct(Number(e.target.value))}
                className="w-14 border border-bb-border bg-black px-1 py-0.5 text-right text-[11px] text-bb-profit outline-none focus:border-bb-amber"
              />
            )}
            {tpOn && <span className="text-[10px] text-bb-muted">%</span>}
          </span>
        </label>
        <label className="flex items-center justify-between gap-2">
          <span className="text-[10px] text-bb-muted" title="Hard exit date (15:55 ET). Empty = +30 day backstop so the enforcer always has an exit.">
            TIME STOP
          </span>
          <input
            type="date" value={timeStopDate}
            onChange={(e) => setTimeStopDate(e.target.value)}
            className="border border-bb-border bg-black px-1 py-0.5 text-[11px] text-bb-orange outline-none focus:border-bb-amber"
          />
        </label>
        <label className="flex items-center justify-between gap-2">
          <span className="text-[10px] text-bb-muted">SHARES (0 = AUTO)</span>
          <input
            data-numeric type="number" step={1} min={0} value={sharesOverride}
            onChange={(e) => setSharesOverride(Math.max(0, Math.floor(Number(e.target.value))))}
            className="w-16 border border-bb-border bg-black px-1 py-0.5 text-right text-[11px] text-white outline-none focus:border-bb-amber"
          />
        </label>
        <label className="flex items-center justify-between gap-2">
          <span className="text-[10px] text-bb-muted" title="DAY limit working the 24/5 extended book (premarket/AH/overnight). RTH-only when off.">
            EXTENDED HOURS
          </span>
          <button
            onClick={() => setExtendedHours(!extendedHours)}
            className={
              "border px-1.5 py-0.5 text-[10px] " +
              (extendedHours ? "border-bb-amber text-bb-amber" : "border-bb-border text-bb-muted")
            }
          >
            {extendedHours ? "ON" : "OFF"}
          </button>
        </label>

        <div className="mt-1 border-t border-bb-border pt-1">
          <Row label="ENTRY (MARKETABLE)" value={price > 0 ? `${side > 0 ? "" : "−"}${price.toFixed(2)}` : "—"} />
          <Row label="SHARES" value={shares > 0 ? `${shares}${sharesOverride > 0 ? "" : " (auto)"}` : "—"} cls="text-bb-amber" />
          <Row label="NOTIONAL" value={notional > 0 ? `$${notional.toLocaleString("en-US", { maximumFractionDigits: 0 })}${side < 0 ? " (1.5× held)" : ""}` : "—"}
            title="Capital charged against the book (shorts at Reg-T 150%)" />
          <Row label="% OF BOOK" value={bookEquity > 0 && notional > 0 ? `${((notional / bookEquity) * 100).toFixed(1)}%` : "—"} />
          <Row label="STOP" value={price > 0 ? Math.abs(exits.sl).toFixed(2) : "—"} cls="text-bb-loss" />
          <Row label="MAX LOSS @ STOP" value={shares > 0 ? `$${maxLoss.toFixed(0)}` : "—"} cls="text-bb-loss"
            title="Planned risk. A gap through the stop (overnight, earnings) exceeds this — size is the only cap there." />
          <Row label="TARGET" value={exits.tp != null ? Math.abs(exits.tp).toFixed(2) : "run (no target)"} cls="text-bb-profit" />
          <Row label="R / R" value={rrMult != null ? `${rrMult.toFixed(1)} : 1` : "open-ended"} cls="text-bb-amber" />
          {book?.enabled && (
            <Row label="BOOK DAY P/L" value={`$${book.realized_today.toFixed(0)} / −$${book.daily_loss_usd.toFixed(0)}`}
              cls={book.realized_today < 0 ? "text-bb-loss" : "text-bb-profit"} />
          )}
        </div>

        {reasons.length > 0 && (
          <div className="flex flex-col gap-0.5 border border-bb-loss/40 p-1">
            {reasons.map((r) => (
              <div key={r} className="text-[10px] text-bb-loss">✗ {r}</div>
            ))}
          </div>
        )}
        {error && <div className="text-[10px] text-bb-loss">{error}</div>}
        {placed && !error && (
          <div className="text-[10px] text-bb-profit">plan {placed.slice(0, 8)} submitted — enforcer armed</div>
        )}

        {!confirming ? (
          <button
            disabled={!canTrade}
            onClick={() => setConfirming(true)}
            className={
              "mt-auto py-1.5 text-[11px] tracking-widest " +
              (canTrade
                ? "bg-bb-amber font-semibold text-black hover:bg-bb-orange"
                : "border border-bb-border text-bb-muted")
            }
          >
            {side > 0 ? "BUY" : "SHORT"} {shares > 0 ? `${shares} ${symbol}` : symbol} (PAPER)
          </button>
        ) : (
          <div className="mt-auto flex gap-px">
            <button
              disabled={submitting}
              onClick={() => void submit()}
              className="flex-1 bg-bb-loss py-1.5 text-[11px] font-semibold tracking-widest text-black"
            >
              {submitting ? "SUBMITTING…" : `CONFIRM ${side > 0 ? "BUY" : "SHORT"} ${shares}`}
            </button>
            <button
              disabled={submitting}
              onClick={() => setConfirming(false)}
              className="border border-bb-border px-3 text-[11px] text-bb-muted"
            >
              CANCEL
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
