/**
 * Manual EQUITY / ETF swing ticket. The discipline is structural: sizing is
 * risk-%-of-account against the stop distance, the stop is REQUIRED (the
 * backend refuses a stopless manual equity entry), the target is optional
 * (let winners run), and every gate shows its refusal reason BEFORE
 * submit. The server re-validates everything.
 *
 * Inputs live in equityTicketStore — the chart draws the same stop /
 * target / exit-day lines and drags them back into this ticket. The plan
 * itself (price, shares, horizon, odds) is derived once in lib/equityPlan
 * for both surfaces.
 *
 * Automation, not habit numbers: the stop suggestion comes from the
 * symbol's own realized vol for the intended hold; the time stop is
 * AUTOMATIC by default — the number of days that stop buys before ordinary
 * noise reaches it; the target chips are 1σ/2σ of the horizon beside the
 * R multiples, with the driftless P(target before stop) stated honestly.
 *
 * One component for both shells, mobile-first: base classes are touch-
 * sized, `sm:` tightens everything for the desktop panel grid.
 */

import { useState } from "react";
import { apiError, postOrder } from "../../lib/api";
import { playCue } from "../../lib/audio";
import { useCapabilities } from "../../lib/capabilities";
import { sharedBars } from "../../lib/chartShared";
import { equityPreflight, swingBackstopUtc } from "../../lib/equityMath";
import { deriveEquityPlan } from "../../lib/equityPlan";
import { etWallToUtcIso } from "../../lib/et";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
import { useEquityTicketStore } from "../../store/equityTicketStore";
import { useTradingStore } from "../../store/tradingStore";

/** ET 15:55 on a calendar date -> UTC ISO. */
function etCloseToUtcIso(dateStr: string): string {
  return etWallToUtcIso(dateStr, "15:55");
}

function fmtDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
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

const stepBtn =
  "h-10 w-10 shrink-0 border border-bb-border text-[16px] leading-none text-bb-muted " +
  "active:bg-bb-amber active:text-black sm:h-5 sm:w-5 sm:text-[11px]";

/** Touch-first numeric control: [−] value [+] steppers plus a real input
 * (inputMode=decimal so phones open the number pad). */
function StepRow({
  label, value, onChange, step, min, max, unit = "%", accent, title,
}: {
  label: string; value: number; onChange: (v: number) => void;
  step: number; min: number; max: number; unit?: string; accent?: string; title?: string;
}) {
  const clamp = (v: number) => Math.min(max, Math.max(min, Math.round(v * 100) / 100));
  return (
    <div className="flex items-center justify-between gap-2 py-0.5" title={title}>
      <span className="text-[11px] text-bb-muted sm:text-[10px]">{label}</span>
      <span className="inline-flex items-center gap-1">
        <button className={stepBtn} onClick={() => onChange(clamp(value - step))} aria-label={`decrease ${label}`}>−</button>
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
        <button className={stepBtn} onClick={() => onChange(clamp(value + step))} aria-label={`increase ${label}`}>+</button>
      </span>
    </div>
  );
}

const chipCls = (on: boolean) =>
  "h-9 border px-2 text-[11px] sm:h-5 sm:px-1.5 sm:text-[10px] " +
  (on ? "border-bb-amber text-bb-amber" : "border-bb-border text-bb-muted active:text-bb-amber");

export function EquityTicket() {
  const symbol = useTradingStore((s) => s.symbol);
  const quote = useTradingStore((s) => s.quote);
  const tf = useTradingStore((s) => s.tf);
  const account = useAccountStore((s) => s.account);
  const refreshAccount = useAccountStore((s) => s.refreshAccount);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const caps = useCapabilities();
  const t = useEquityTicketStore();

  const [more, setMore] = useState(false);
  // A pending confirm / result belongs to the ticket revision it was made
  // on; any edit (from here or the chart) leaves it behind — no effects.
  const [confirmRev, setConfirmRev] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<{ rev: number; msg: string } | null>(null);
  const [placed, setPlaced] = useState<{ rev: number; id: string } | null>(null);
  const confirming = confirmRev === t.rev;
  const errorMsg = error && error.rev === t.rev ? error.msg : null;
  const placedId = placed && placed.rev === t.rev ? placed.id : null;

  const plan = deriveEquityPlan({ ticket: t, quote, bars: sharedBars.current, tf, account });
  const { price, exits, shares } = plan;
  const longOnly = !caps.shortsAllowed;
  const reasons = equityPreflight({
    price, side: t.side, shares, exits, maxLossCapUsd: plan.maxLossCapUsd,
    equityLongOnly: longOnly, quoteFresh: plan.quoteFresh,
  });
  // Nothing submits until the server mode has loaded — an unloaded ticket
  // on the live server would send a real order under a PAPER label.
  const { live, loaded } = useTradingMode();
  const canTrade = loaded && reasons.length === 0 && shares > 0 && !submitting;
  const sideWord = t.side > 0 ? "BUY" : "SHORT";
  const modeWord = live ? "LIVE" : "PAPER";

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    const rev = t.rev;
    try {
      const created = await postOrder({
        underlying: symbol,
        strategy: "manual_equity",
        asset_class: "equity",
        extended_hours: t.extendedHours,
        legs: [{
          symbol,
          side: t.side,
          ratio: 1,
          entry: Number(exits.entry.toFixed(2)),
          half_spread: plan.halfSpread != null ? Number(plan.halfSpread.toFixed(4)) : null,
        }],
        qty: shares,
        entry_limit: Number(exits.entry.toFixed(2)),
        tp_premium: exits.tp,
        sl_premium: exits.sl,
        time_stop_utc: plan.timeStopDate ? etCloseToUtcIso(plan.timeStopDate) : swingBackstopUtc(),
      });
      playCue("submitted");
      setPlaced({ rev, id: created.id });
      setConfirmRev(null);
      void refreshPositions();
      void refreshAccount();
    } catch (err) {
      // Server-side risk rejections never create a plan, so no WS cue fires
      // for them — buzz locally.
      playCue("rejected");
      setError({ rev, msg: apiError(err) });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="panel relative flex min-w-0 flex-col">
      <div className="panel-title flex items-center justify-between">
        <span>EQUITY SWING TICKET</span>
        {plan.acctEquity > 0 && (
          <span
            className="text-[10px] tracking-normal text-bb-muted sm:text-[9px]"
            title="The SELECTED account's equity — every %-gate binds here. Switch accounts on the ACCOUNT page (restart applies)."
          >
            ACCT ${Math.round(plan.acctEquity).toLocaleString()}
          </span>
        )}
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto p-2 sm:gap-1">
        {/* Direction: the one decision that recolors everything below it.
            A long-only account (cash / IRA) has no SHORT side at all. */}
        <div className="flex gap-px">
          {(longOnly ? ([1] as const) : ([1, -1] as const)).map((s) => (
            <button
              key={s}
              onClick={() => t.setSide(s)}
              className={
                "h-11 flex-1 text-[13px] tracking-widest sm:h-7 sm:text-[11px] " +
                (t.side === s
                  ? s > 0
                    ? "bg-bb-profit font-semibold text-black"
                    : "bg-bb-loss font-semibold text-black"
                  : "border border-bb-border text-bb-muted active:text-bb-amber")
              }
            >
              {s > 0 ? (longOnly ? "LONG · long-only account" : "LONG") : "SHORT"}
            </button>
          ))}
        </div>

        {/* The two numbers that ARE the trade plan. */}
        <StepRow label="RISK (% OF ACCT)" value={t.riskPct} onChange={t.setRiskPct}
          step={0.25} min={0.1} max={10} accent="text-bb-amber"
          title={`Risk budget: $${plan.riskBudget.toFixed(0)} of the $${plan.acctEquity.toLocaleString()} account`} />
        <StepRow label="STOP DISTANCE" value={t.slPct} onChange={t.setSlPct}
          step={0.5} min={0.5} max={50} accent="text-bb-loss"
          title="Hard stop below entry. The enforcer executes it — entries without a stop are refused. Drag the red line on the chart to move it." />
        {plan.stopSuggestion != null && (
          <div className="flex items-center justify-between gap-2">
            <button
              onClick={() => t.setSlPct(plan.stopSuggestion!)}
              className={
                "h-9 border px-2 text-[11px] sm:h-5 sm:px-1.5 sm:text-[10px] " +
                (Math.abs(t.slPct - plan.stopSuggestion) < 0.26
                  ? "border-bb-profit text-bb-profit"
                  : "border-bb-border text-bb-muted active:text-bb-amber")
              }
              title={
                `Suggested stop for a ~${plan.holdDays}-trading-day hold: 1.5× the ` +
                `hold-horizon 1σ (daily σ ${plan.dSigma.toFixed(1)}% from this chart's ` +
                `realized vol). Outside ordinary noise, inside disaster range.`
              }
            >
              SUGGEST {plan.stopSuggestion}% (1.5σ·{plan.holdDays}d)
            </button>
            {plan.stopInsideNoise && (
              <span
                className="text-right text-[10px] text-bb-orange sm:text-[9px]"
                title="House wick study: winners routinely trade ~1σ against entry before paying — a stop inside the noise band mostly harvests shakeouts."
              >
                ⚠ inside {plan.holdDays}d noise (1σ≈{plan.noiseSigma.toFixed(1)}%)
              </span>
            )}
          </div>
        )}

        {/* Horizon: automatic by default — how long this stop stays outside
            the symbol's own noise. A date makes it manual; AUTO restores. */}
        <div className="flex items-center justify-between gap-2 py-0.5">
          <span
            className="text-[11px] text-bb-muted sm:text-[10px]"
            title="Hard exit at 15:55 ET on this date. AUTO = the trading days a stop of this width buys at 1.5σ of the symbol's realized vol (drag the orange line on the chart)."
          >
            TIME STOP
          </span>
          <span className="inline-flex items-center gap-1">
            <button
              onClick={() => t.setAutoTimeStop(true)}
              className={chipCls(plan.timeStopAuto)}
              title={plan.dSigma > 0 ? `stop ${t.slPct}% ≈ 1.5σ over ${plan.holdDays} trading days` : "no vol read yet — 5-day default"}
            >
              AUTO{plan.timeStopAuto ? ` · ${fmtDate(plan.timeStopDate)} (${plan.holdDays}d)` : ""}
            </button>
            <input
              type="date"
              value={t.timeStopDate}
              onChange={(e) => t.setTimeStopDate(e.target.value)}
              className={
                "h-9 border bg-black px-1 text-[12px] outline-none focus:border-bb-amber sm:h-5 sm:text-[11px] " +
                (t.timeStopDate ? "border-bb-orange text-bb-orange" : "border-bb-border text-bb-muted")
              }
              aria-label="Time stop date"
            />
          </span>
        </div>

        {/* Target: σ-of-horizon chips beside the R multiples; RUN = none. */}
        <div className="flex items-center justify-between gap-2 py-0.5">
          <span className="text-[11px] text-bb-muted sm:text-[10px]" title="Optional. Drag the green line on the chart to move it.">
            TARGET
          </span>
          <span className="inline-flex flex-wrap items-center justify-end gap-1">
            <button onClick={() => t.setTarget(false)} className={chipCls(!t.tpOn)} title="No target — let the winner run under the hard stop">
              RUN
            </button>
            {plan.sigmaTargets &&
              ([1, 2] as const).map((k) => (
                <button
                  key={k}
                  onClick={() => t.setTarget(true, plan.sigmaTargets![k - 1])}
                  className={chipCls(t.tpOn && Math.abs(t.tpPct - plan.sigmaTargets![k - 1]) < 0.26)}
                  title={`${k}σ expected move over the ${plan.holdDays}-day horizon (${plan.sigmaTargets![k - 1]}%)`}
                >
                  {k}σ
                </button>
              ))}
            {[2, 3].map((mult) => (
              <button
                key={mult}
                onClick={() => t.setTarget(true, Math.round(t.slPct * mult * 2) / 2)}
                className={chipCls(t.tpOn && Math.abs(t.tpPct - Math.round(t.slPct * mult * 2) / 2) < 0.26)}
                title={`Target at ${mult}R (${mult}× the stop distance)`}
              >
                {mult}R
              </button>
            ))}
            {t.tpOn && (
              <span className="inline-flex items-center gap-1">
                <button className={stepBtn} onClick={() => t.setTpPct(t.tpPct - 1)} aria-label="decrease target">−</button>
                <span data-numeric className="w-12 text-center text-[13px] text-bb-profit sm:text-[11px]">+{t.tpPct}%</span>
                <button className={stepBtn} onClick={() => t.setTpPct(t.tpPct + 1)} aria-label="increase target">+</button>
              </span>
            )}
          </span>
        </div>
        {t.tpOn && plan.pTarget != null && (
          <div
            className="text-right text-[10px] text-bb-muted sm:text-[9px]"
            title="Driftless (martingale) log-price: probability the target is touched before the stop, ignoring the time stop. Independent of vol — it is the log distances alone. Any edge you have is on top of this."
          >
            P(target before stop) ≈ <span className={plan.pTarget >= 0.5 ? "text-bb-profit" : "text-bb-orange"}>{(plan.pTarget * 100).toFixed(0)}%</span> driftless
          </div>
        )}

        {/* Advanced fields fold away — the default ticket is 3 decisions. */}
        <button
          onClick={() => setMore(!more)}
          className="self-start py-1 text-[11px] tracking-widest text-bb-muted active:text-bb-amber sm:py-0 sm:text-[10px]"
        >
          {more ? "▾ LESS" : "▸ MORE (shares · ext-hours)"}
        </button>
        {more && (
          <div className="flex flex-col gap-1.5 border-l border-bb-border pl-2 sm:gap-1">
            <label className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-bb-muted sm:text-[10px]">SHARES (0 = AUTO {plan.autoShares})</span>
              <input
                data-numeric type="number" inputMode="numeric" step={1} min={0} value={t.sharesOverride}
                onChange={(e) => t.setSharesOverride(Number(e.target.value))}
                className="h-10 w-20 border border-bb-border bg-black px-1 text-right text-[14px] text-white outline-none focus:border-bb-amber sm:h-5 sm:w-16 sm:text-[11px]"
              />
            </label>
            <label className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-bb-muted sm:text-[10px]"
                title="DAY limit working the 24/5 extended book (premarket/AH/overnight). RTH-only when off.">
                EXTENDED HOURS
              </span>
              <button onClick={() => t.setExtendedHours(!t.extendedHours)} className={chipCls(t.extendedHours)}>
                {t.extendedHours ? "ON" : "OFF"}
              </button>
            </label>
          </div>
        )}

        {/* Derived plan — the four numbers that matter, big; context muted. */}
        <div className="mt-1 border-t border-bb-border pt-1.5 sm:pt-1">
          <Row big label={`${sideWord} ${symbol}`} value={price > 0 ? `${shares} sh @ ${price.toFixed(2)}` : "—"} cls="text-bb-amber" />
          <Row big label="MAX LOSS @ STOP" value={shares > 0 ? `$${plan.maxLoss.toFixed(0)}` : "—"} cls="text-bb-loss"
            title="Planned risk. A gap through the stop (overnight, earnings) exceeds this — size is the only cap there." />
          <Row label="STOP / TARGET"
            value={price > 0 ? `${Math.abs(exits.sl).toFixed(2)} / ${exits.tp != null ? Math.abs(exits.tp).toFixed(2) : "run"}` : "—"} />
          <Row label="R / R" value={plan.rrMult != null ? `${plan.rrMult.toFixed(1)} : 1` : "open-ended"} />
          <Row label="EXIT" value={plan.timeStopDate ? `${fmtDate(plan.timeStopDate)} 15:55 ET${plan.timeStopAuto ? " (auto)" : ""}` : "30d backstop"} cls="text-bb-orange" />
          <Row label="NOTIONAL · % OF ACCT"
            value={plan.notional > 0 ? `$${plan.notional.toLocaleString("en-US", { maximumFractionDigits: 0 })}${t.side < 0 ? " (1.5×)" : ""} · ${plan.acctEquity > 0 ? ((plan.notional / plan.acctEquity) * 100).toFixed(1) : "—"}%` : "—"} />
          {account && plan.dailyCapUsd != null && (
            <Row label="ACCT DAY P/L"
              value={`$${account.day_realized_pnl.toFixed(0)} / −$${plan.dailyCapUsd.toFixed(0)}`}
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
        {errorMsg && (
          <div className="border border-bb-loss/60 p-1.5 text-[11px] text-bb-loss sm:p-1 sm:text-[10px]">
            ✗ broker/engine refused: {errorMsg}
          </div>
        )}
        {placedId && !errorMsg && (
          <div className="border border-bb-profit/60 p-1.5 text-[11px] text-bb-profit sm:p-1 sm:text-[10px]">
            ✓ plan {placedId.slice(0, 8)} submitted — enforcer armed (stop is live)
          </div>
        )}

        {/* Two-step submit: the confirm restates the whole contract. */}
        {!confirming ? (
          <button
            disabled={!canTrade}
            onClick={() => setConfirmRev(t.rev)}
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
              {sideWord} {shares} {symbol} @ ≤{price.toFixed(2)} · stop {Math.abs(exits.sl).toFixed(2)} (−${plan.maxLoss.toFixed(0)})
              {exits.tp != null ? ` · target ${Math.abs(exits.tp).toFixed(2)}` : " · no target (run)"}
              {plan.timeStopDate ? ` · exit ${plan.timeStopDate}` : " · 30d backstop"}
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
                onClick={() => setConfirmRev(null)}
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
