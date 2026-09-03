/**
 * Manual EQUITY / ETF swing ticket. The discipline is structural: sizing is
 * risk-%-of-the-manual-book against the stop distance, the stop is REQUIRED
 * (the backend refuses a stopless manual equity entry), the target is
 * optional (let winners run), and every gate shows its refusal reason
 * BEFORE submit. The server re-validates everything.
 *
 * One component for both shells, mobile-first: base classes are
 * touch-sized (44px targets, steppers instead of bare number spinners,
 * advanced fields behind MORE), `sm:` tightens everything for the desktop
 * panel grid. Submit is a two-step with an explicit summary — the confirm
 * shows exactly what the enforcer will hold you to.
 */

import { useState } from "react";
import { apiError, postOrder } from "../../lib/api";
import { playCue } from "../../lib/audio";
import { sharedBars } from "../../lib/chartShared";
import { etWallToUtcIso } from "../../lib/et";
import {
  capitalUsd,
  dailySigmaPct,
  equityExits,
  equityPreflight,
  holdDaysUntil,
  holdSigmaPct,
  rr,
  sharesForRisk,
  suggestedStopPct,
  swingBackstopUtc,
} from "../../lib/equityMath";
import { realizedVolAnnualized } from "../../lib/indicators";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
import { freshSpot, quoteIsStale, TF_MS, useTradingStore } from "../../store/tradingStore";

/** ET 15:55 on a calendar date -> UTC ISO. */
function etCloseToUtcIso(dateStr: string): string {
  return etWallToUtcIso(dateStr, "15:55");
}

function Row({ label, value, cls, title, big }: {
  label: string; value: string; cls?: string; title?: string; big?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-2" title={title}>
      <span className="text-[11px] text-bb-muted sm:text-[10px]">{label}</span>
      <span
        data-numeric
        className={
          (big ? "text-[15px] sm:text-[12px] " : "text-[12px] sm:text-[11px] ") +
          (cls ?? "text-white")
        }
      >
        {value}
      </span>
    </div>
  );
}

/** Touch-first numeric control: [−] value [+] steppers plus a real input
 * (inputMode=decimal so phones open the number pad). */
function StepRow({
  label, value, onChange, step, min, max, unit = "%", accent, title,
}: {
  label: string; value: number; onChange: (v: number) => void;
  step: number; min: number; max: number; unit?: string; accent?: string; title?: string;
}) {
  const clamp = (v: number) => Math.min(max, Math.max(min, Math.round(v * 100) / 100));
  const btn =
    "h-10 w-10 shrink-0 border border-bb-border text-[16px] leading-none text-bb-muted " +
    "active:bg-bb-amber active:text-black sm:h-5 sm:w-5 sm:text-[11px]";
  return (
    <div className="flex items-center justify-between gap-2 py-0.5" title={title}>
      <span className="text-[11px] text-bb-muted sm:text-[10px]">{label}</span>
      <span className="inline-flex items-center gap-1">
        <button className={btn} onClick={() => onChange(clamp(value - step))} aria-label={`decrease ${label}`}>−</button>
        <span className="inline-flex items-center gap-0.5">
          <input
            data-numeric
            type="number"
            inputMode="decimal"
            step={step}
            value={value}
            onChange={(e) => onChange(clamp(Number(e.target.value)))}
            className={
              "h-10 w-16 border border-bb-border bg-black px-1 text-center text-[14px] outline-none " +
              "focus:border-bb-amber sm:h-5 sm:w-14 sm:text-[11px] " + (accent ?? "text-white")
            }
          />
          <span className="text-[11px] text-bb-muted sm:text-[10px]">{unit}</span>
        </span>
        <button className={btn} onClick={() => onChange(clamp(value + step))} aria-label={`increase ${label}`}>+</button>
      </span>
    </div>
  );
}

export function EquityTicket() {
  const symbol = useTradingStore((s) => s.symbol);
  const quote = useTradingStore((s) => s.quote);
  const tf = useTradingStore((s) => s.tf);
  const account = useAccountStore((s) => s.account);
  const refreshAccount = useAccountStore((s) => s.refreshAccount);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);

  const [side, setSide] = useState<1 | -1>(1);
  const [riskPct, setRiskPct] = useState(1.0);
  const [slPct, setSlPct] = useState(5.0);
  const [tpOn, setTpOn] = useState(false);
  const [tpPct, setTpPct] = useState(10.0);
  const [more, setMore] = useState(false);
  const [sharesOverride, setSharesOverride] = useState(0); // 0 = auto
  const [timeStopDate, setTimeStopDate] = useState(""); // "" = +30d backstop
  const [extendedHours, setExtendedHours] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [placed, setPlaced] = useState<string | null>(null);

  // Sizing denominates in the SELECTED account's real equity — capital
  // separation is done with real accounts (one per book), so a $11k share
  // account makes every %-gate bind at $11k by construction.
  const quoteFresh = !!quote && quote.mid > 0 && !quoteIsStale(quote);
  // Marketable pricing: pay the ask long, hit the bid short; mid fallback.
  const rawPx = side > 0 ? quote?.ask : quote?.bid;
  const price = quoteFresh && rawPx && rawPx > 0 ? rawPx : freshSpot(quote, 0);
  const halfSpread =
    quote && quote.ask > 0 && quote.bid > 0 && quote.ask >= quote.bid
      ? (quote.ask - quote.bid) / 2
      : null;

  const exits = equityExits(price, side, slPct / 100, tpOn ? tpPct / 100 : null);
  const acctEquity = account?.equity ?? 0;
  const maxLossCapUsd =
    account && account.risk ? account.equity * account.risk.max_loss_pct : null;
  const dailyCapUsd =
    account && account.risk ? account.equity * account.risk.daily_loss_pct : null;
  const riskBudget = (acctEquity * riskPct) / 100;

  // Vol-scaled stop intelligence off the symbol's own tape (chart bars).
  const bars = sharedBars.current;
  const rv = bars.n > 30 ? realizedVolAnnualized(bars, 30, TF_MS[tf] / 60_000) : 0;
  const dSigma = dailySigmaPct(rv);
  const holdDays = timeStopDate ? holdDaysUntil(timeStopDate) : 5; // default swing horizon
  const stopSuggestion = suggestedStopPct(dSigma, holdDays);
  const noiseSigma = dSigma > 0 ? holdSigmaPct(dSigma, holdDays) : 0;
  const stopInsideNoise = noiseSigma > 0 && slPct < noiseSigma;
  const autoShares = price > 0 ? sharesForRisk(riskBudget, exits.entry, exits.sl) : 0;
  const shares = sharesOverride > 0 ? Math.min(sharesOverride, Math.max(autoShares, 1)) : autoShares;
  const notional = capitalUsd(price, shares, side);
  const maxLoss = (exits.entry - exits.sl) * shares;
  const rrMult = rr(exits.entry, exits.tp, exits.sl);
  const longOnly = account?.risk?.equity_long_only ?? true;

  const reasons = equityPreflight({
    price, side, shares, exits, maxLossCapUsd,
    equityLongOnly: longOnly,
    quoteFresh,
  });
  // Nothing submits until the server mode has loaded — an unloaded ticket
  // on the live server would send a real order under a PAPER label.
  const { live, loaded } = useTradingMode();
  const canTrade = loaded && reasons.length === 0 && shares > 0 && !submitting;
  const sideWord = side > 0 ? "BUY" : "SHORT";
  const modeWord = live ? "LIVE" : "PAPER";

  const edit = <T,>(setter: (v: T) => void) => (v: T) => {
    // Any edit invalidates a pending confirm and clears stale feedback —
    // the confirm summary must never describe a different trade.
    setConfirming(false);
    setError(null);
    setPlaced(null);
    setter(v);
  };

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
      playCue("submitted");
      setPlaced(plan.id);
      setConfirming(false);
      void refreshPositions();
      void refreshAccount();
    } catch (err) {
      // Server-side risk rejections never create a plan, so no WS cue fires
      // for them — buzz locally.
      playCue("rejected");
      setError(apiError(err));
    } finally {
      setSubmitting(false);
    }
  };

  const toggleBtn = (on: boolean) =>
    "h-10 min-w-14 border px-2 text-[11px] sm:h-5 sm:min-w-0 sm:px-1.5 sm:text-[10px] " +
    (on ? "border-bb-amber text-bb-amber" : "border-bb-border text-bb-muted");

  return (
    <div className="panel relative flex min-w-0 flex-col">
      <div className="panel-title flex items-center justify-between">
        <span>EQUITY SWING TICKET</span>
        {acctEquity > 0 && (
          <span
            className="text-[10px] tracking-normal text-bb-muted sm:text-[9px]"
            title="The SELECTED account's equity — every %-gate binds here. Switch accounts in the ⚙ SYSTEM drawer (restart applies)."
          >
            ACCT ${Math.round(acctEquity).toLocaleString()}
          </span>
        )}
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto p-2 sm:gap-1">
        {/* Direction: the one decision that recolors everything below it. */}
        <div className="flex gap-px">
          {([1, -1] as const).map((s) => (
            <button
              key={s}
              onClick={() => edit(setSide)(s)}
              className={
                "h-11 flex-1 text-[13px] tracking-widest sm:h-7 sm:text-[11px] " +
                (side === s
                  ? s > 0
                    ? "bg-bb-profit font-semibold text-black"
                    : "bg-bb-loss font-semibold text-black"
                  : "border border-bb-border text-bb-muted active:text-bb-amber")
              }
            >
              {s > 0 ? "LONG" : "SHORT"}
            </button>
          ))}
        </div>
        {side < 0 && longOnly && (
          <div className="text-[11px] text-bb-orange sm:text-[10px]">
            shorts are gated off (equity_long_only) — long-only book
          </div>
        )}

        {/* The two numbers that ARE the trade plan. */}
        <StepRow label="RISK (% OF ACCT)" value={riskPct} onChange={edit(setRiskPct)}
          step={0.25} min={0.1} max={10} accent="text-bb-amber"
          title={`Risk budget: $${riskBudget.toFixed(0)} of the $${acctEquity.toLocaleString()} account`} />
        <StepRow label="STOP DISTANCE" value={slPct} onChange={edit(setSlPct)}
          step={0.5} min={0.5} max={50} accent="text-bb-loss"
          title="Hard stop below entry. The enforcer executes it — entries without a stop are refused." />
        {/* Vol-scaled stop intelligence: suggestion from the symbol's OWN
            measured volatility for the intended hold, and a warning when
            the chosen stop sits inside ordinary noise (shakeout-prone). */}
        {stopSuggestion != null && (
          <div className="flex items-center justify-between gap-2">
            <button
              onClick={() => edit(setSlPct)(stopSuggestion)}
              className={
                "h-9 border px-2 text-[11px] sm:h-5 sm:px-1.5 sm:text-[10px] " +
                (Math.abs(slPct - stopSuggestion) < 0.26
                  ? "border-bb-profit text-bb-profit"
                  : "border-bb-border text-bb-muted active:text-bb-amber")
              }
              title={
                `Suggested stop for a ~${holdDays}-trading-day hold: 1.5× the ` +
                `hold-horizon 1σ (daily σ ${dSigma.toFixed(1)}% from this chart's ` +
                `realized vol). Outside ordinary noise, inside disaster range.`
              }
            >
              SUGGEST {stopSuggestion}% (1.5σ·{holdDays}d)
            </button>
            {stopInsideNoise && (
              <span
                className="text-right text-[10px] text-bb-orange sm:text-[9px]"
                title="House wick study: winners routinely trade ~1σ against entry before paying — a stop inside the noise band mostly harvests shakeouts."
              >
                ⚠ inside {holdDays}d noise (1σ≈{noiseSigma.toFixed(1)}%)
              </span>
            )}
          </div>
        )}
        <div className="flex items-center justify-between gap-2 py-0.5">
          <span className="text-[11px] text-bb-muted sm:text-[10px]">TARGET</span>
          <span className="inline-flex items-center gap-1">
            {[2, 3].map((mult) => (
              <button
                key={mult}
                onClick={() => {
                  edit(setTpOn)(true);
                  setTpPct(Math.round(slPct * mult * 2) / 2);
                }}
                className={
                  "h-9 border border-bb-border px-1.5 text-[10px] text-bb-muted active:text-bb-amber sm:h-5 sm:px-1 sm:text-[9px]"
                }
                title={`Set target at ${mult}R (${mult}× the stop distance)`}
              >
                {mult}R
              </button>
            ))}
            <button
              onClick={() => edit(setTpOn)(!tpOn)}
              className={toggleBtn(tpOn)}
              title="Optional: RUN = no target, let the winner run under the hard stop"
            >
              {tpOn ? `+${tpPct}%` : "RUN"}
            </button>
            {tpOn && (
              <span className="inline-flex items-center gap-1">
                <button className="h-10 w-10 border border-bb-border text-[16px] text-bb-muted active:bg-bb-amber active:text-black sm:h-5 sm:w-5 sm:text-[11px]"
                  onClick={() => edit(setTpPct)(Math.max(1, tpPct - 1))} aria-label="decrease target">−</button>
                <button className="h-10 w-10 border border-bb-border text-[16px] text-bb-muted active:bg-bb-amber active:text-black sm:h-5 sm:w-5 sm:text-[11px]"
                  onClick={() => edit(setTpPct)(Math.min(200, tpPct + 1))} aria-label="increase target">+</button>
              </span>
            )}
          </span>
        </div>

        {/* Advanced fields fold away — the default ticket is 3 decisions. */}
        <button
          onClick={() => setMore(!more)}
          className="self-start py-1 text-[11px] tracking-widest text-bb-muted active:text-bb-amber sm:py-0 sm:text-[10px]"
        >
          {more ? "▾ LESS" : "▸ MORE (time stop · shares · ext-hours)"}
        </button>
        {more && (
          <div className="flex flex-col gap-1.5 border-l border-bb-border pl-2 sm:gap-1">
            <label className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-bb-muted sm:text-[10px]"
                title="Hard exit date (15:55 ET). Empty = +30 day backstop so the enforcer always has an exit.">
                TIME STOP
              </span>
              <input
                type="date" value={timeStopDate}
                onChange={(e) => edit(setTimeStopDate)(e.target.value)}
                className="h-10 border border-bb-border bg-black px-1 text-[12px] text-bb-orange outline-none focus:border-bb-amber sm:h-5 sm:text-[11px]"
              />
            </label>
            <label className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-bb-muted sm:text-[10px]">SHARES (0 = AUTO)</span>
              <input
                data-numeric type="number" inputMode="numeric" step={1} min={0} value={sharesOverride}
                onChange={(e) => edit(setSharesOverride)(Math.max(0, Math.floor(Number(e.target.value))))}
                className="h-10 w-20 border border-bb-border bg-black px-1 text-right text-[14px] text-white outline-none focus:border-bb-amber sm:h-5 sm:w-16 sm:text-[11px]"
              />
            </label>
            <label className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-bb-muted sm:text-[10px]"
                title="DAY limit working the 24/5 extended book (premarket/AH/overnight). RTH-only when off.">
                EXTENDED HOURS
              </span>
              <button onClick={() => edit(setExtendedHours)(!extendedHours)} className={toggleBtn(extendedHours)}>
                {extendedHours ? "ON" : "OFF"}
              </button>
            </label>
          </div>
        )}

        {/* Derived plan — the four numbers that matter, big; context muted. */}
        <div className="mt-1 border-t border-bb-border pt-1.5 sm:pt-1">
          <Row big label={`${sideWord} ${symbol}`} value={price > 0 ? `${shares} sh @ ${price.toFixed(2)}` : "—"} cls="text-bb-amber" />
          <Row big label="MAX LOSS @ STOP" value={shares > 0 ? `$${maxLoss.toFixed(0)}` : "—"} cls="text-bb-loss"
            title="Planned risk. A gap through the stop (overnight, earnings) exceeds this — size is the only cap there." />
          <Row label="STOP / TARGET"
            value={price > 0 ? `${Math.abs(exits.sl).toFixed(2)} / ${exits.tp != null ? Math.abs(exits.tp).toFixed(2) : "run"}` : "—"} />
          <Row label="R / R" value={rrMult != null ? `${rrMult.toFixed(1)} : 1` : "open-ended"} />
          <Row label="NOTIONAL · % OF ACCT"
            value={notional > 0 ? `$${notional.toLocaleString("en-US", { maximumFractionDigits: 0 })}${side < 0 ? " (1.5×)" : ""} · ${acctEquity > 0 ? ((notional / acctEquity) * 100).toFixed(1) : "—"}%` : "—"} />
          {account && dailyCapUsd != null && (
            <Row label="ACCT DAY P/L"
              value={`$${account.day_realized_pnl.toFixed(0)} / −$${dailyCapUsd.toFixed(0)}`}
              title="Realized today vs the account's daily circuit breaker (daily_loss_pct)"
              cls={account.day_realized_pnl < 0 ? "text-bb-loss" : "text-bb-profit"} />
          )}
        </div>

        {/* Feedback zone: refusals BEFORE submit, one state at a time. */}
        {reasons.length > 0 && (
          <div className="flex flex-col gap-0.5 border border-bb-loss/40 p-1.5 sm:p-1">
            {reasons.map((r) => (
              <div key={r} className="text-[11px] text-bb-loss sm:text-[10px]">✗ {r}</div>
            ))}
          </div>
        )}
        {error && (
          <div className="border border-bb-loss/60 p-1.5 text-[11px] text-bb-loss sm:p-1 sm:text-[10px]">
            ✗ broker/engine refused: {error}
          </div>
        )}
        {placed && !error && (
          <div className="border border-bb-profit/60 p-1.5 text-[11px] text-bb-profit sm:p-1 sm:text-[10px]">
            ✓ plan {placed.slice(0, 8)} submitted — enforcer armed (stop is live)
          </div>
        )}

        {/* Two-step submit: the confirm restates the whole contract. */}
        {!confirming ? (
          <button
            disabled={!canTrade}
            onClick={() => setConfirming(true)}
            className={
              "mt-auto h-12 text-[13px] tracking-widest sm:h-8 sm:text-[11px] " +
              (canTrade
                ? live
                  ? "bg-bb-loss font-semibold text-black active:bg-bb-orange"
                  : "bg-bb-amber font-semibold text-black active:bg-bb-orange"
                : "border border-bb-border text-bb-muted")
            }
          >
            {canTrade
              ? `${sideWord} ${shares} ${symbol} (${modeWord})`
              : reasons.length
                ? `BLOCKED — ${reasons.length} ${reasons.length === 1 ? "REASON" : "REASONS"} ABOVE`
                : `${sideWord} ${symbol}`}
          </button>
        ) : (
          <div className="mt-auto flex flex-col gap-1">
            <div
              className={
                live
                  ? "border border-bb-loss bg-bb-loss/10 p-1.5 text-[11px] text-bb-loss sm:text-[10px]"
                  : "border border-bb-amber/60 bg-bb-amber/10 p-1.5 text-[11px] text-bb-amber sm:text-[10px]"
              }
            >
              {live && <span className="font-semibold">LIVE ORDER — REAL MONEY · </span>}
              {sideWord} {shares} {symbol} @ ≤{price.toFixed(2)} · stop {Math.abs(exits.sl).toFixed(2)} (−${maxLoss.toFixed(0)})
              {exits.tp != null ? ` · target ${Math.abs(exits.tp).toFixed(2)}` : " · no target (run)"}
              {timeStopDate ? ` · exit ${timeStopDate}` : " · 30d backstop"}
            </div>
            <div className="flex gap-px">
              <button
                disabled={submitting}
                onClick={() => void submit()}
                className="h-12 flex-[2] bg-bb-loss text-[13px] font-semibold tracking-widest text-black active:bg-bb-orange sm:h-8 sm:text-[11px]"
              >
                {submitting ? "SUBMITTING…" : "CONFIRM"}
              </button>
              <button
                disabled={submitting}
                onClick={() => setConfirming(false)}
                className="h-12 flex-1 border border-bb-border text-[12px] text-bb-muted sm:h-8 sm:text-[11px]"
              >
                CANCEL
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
