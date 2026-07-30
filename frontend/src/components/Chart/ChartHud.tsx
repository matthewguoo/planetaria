/**
 * Upper-left on-chart HUD: overlay/sim switches plus the probability &
 * Monte Carlo readout, rendered as an HTML layer over the canvas so the
 * numbers live where the eyes are — on the chart, not in a bottom panel.
 * The container ignores pointer events; only its controls capture them, so
 * chart pan/zoom/drag pass straight through.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  getPlanEvents,
  getSystemState,
  type Plan,
  type PlanEvent,
  type SystemState,
} from "../../lib/api";
import { computeAtr, realizedVolAnnualized } from "../../lib/indicators";
import { buildPositionView, computeExcursions } from "../../lib/positionView";
import type { McResult } from "../../lib/mcSim";
import { bsThetaPerDay, structuralMaxLoss, TRADING_HOURS_PER_YEAR } from "../../lib/optionsMath";
import { useMonteCarlo, type McInputs } from "../../lib/useMonteCarlo";
import type { Designer } from "../../lib/useDesigner";
import { useAccountStore } from "../../store/accountStore";
import { THETA_TEMPLATES, useStrategyStore } from "../../store/strategyStore";
import { TF_MS, useTradingStore } from "../../store/tradingStore";
import { useUiStore } from "../../store/uiStore";
import type { Bars } from "./scales";

const TOGGLES = [
  { key: "heat", label: "HEAT", title: "P/L heatmap surface (price × time)" },
  { key: "sim", label: "SIM", title: "Probability + Monte Carlo readout" },
  { key: "theta", label: "THETA", title: "Theta-sell overlay: expected-move cone + delta-targeted premium-selling templates" },
  { key: "vwap", label: "VWAP", title: "Session-anchored VWAP" },
  { key: "ema", label: "EMA", title: "EMA 9 / 21" },
  { key: "bb", label: "BB", title: "Bollinger 20 × 2σ" },
] as const;

const pct = (v: number | null) => (v === null ? "—" : `${(v * 100).toFixed(0)}%`);
const usd = (v: number) => `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(0)}`;

function StatRow({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-bb-muted">{label}</span>
      <span data-numeric className={cls ?? "text-white"}>
        {value}
      </span>
    </div>
  );
}

function etCountdown(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.parse(iso) - Date.now();
  if (ms <= 0) return "due";
  const m = Math.floor(ms / 60000);
  return m >= 90 ? `${(m / 60).toFixed(1)}h` : `${m}m`;
}

/** Post-trade report for a CLOSED plan viewed on the chart: realized P/L,
 * MAE/MFE reconstructed from the 1m bars over the holding window (entry-basis
 * pricing — consistent with the surface), exit reason, time in trade. */
function HistoricalBlock({
  plan,
  barsRef,
}: {
  plan: Plan;
  barsRef: React.RefObject<Bars>;
}) {
  // Bars stream in after the symbol switch; re-evaluate periodically.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 2000);
    return () => window.clearInterval(id);
  }, []);

  const view = useMemo(() => buildPositionView(plan, null, "entry"), [plan]);
  const bars = barsRef.current;
  const excursions =
    view && bars && bars.n ? computeExcursions(view, bars) : null;
  if (!view) return null;

  const qty = view.qty;
  const holdMs = (view.exitMs ?? Date.now()) - view.anchorMs;
  const holdLabel =
    holdMs >= 90 * 60_000
      ? `${(holdMs / 3_600_000).toFixed(1)}h`
      : `${Math.max(Math.round(holdMs / 60_000), 1)}m`;
  const basisDollars = Math.abs(view.entryBasis) * 100 * qty;
  const pctOf = (v: number) =>
    basisDollars >= 1 ? ` (${v >= 0 ? "+" : ""}${((v / basisDollars) * 100).toFixed(0)}%)` : "";
  const win = (plan.realized_pnl ?? 0) >= 0;

  return (
    <div
      className="pointer-events-none flex flex-col gap-0.5 border border-bb-neutral/50 bg-black/80 px-1.5 py-1"
      title="Closed-trade replay: MAE/MFE are model-reconstructed from 1m bars at the entry basis (frozen fill IVs); exit marker on the chart is the actual exit event"
    >
      <div className="flex justify-between gap-2">
        <span className="tracking-widest text-bb-neutral">CLOSED TRADE</span>
        <span className={win ? "text-bb-profit" : "text-bb-loss"}>
          {(plan.exit_reason ?? plan.status).toUpperCase()}
        </span>
      </div>
      <StatRow
        label="REALIZED"
        value={
          plan.realized_pnl !== null && plan.realized_pnl !== undefined
            ? `${usd(plan.realized_pnl * 1)}${pctOf(plan.realized_pnl)}`
            : "—"
        }
        cls={win ? "text-bb-profit" : "text-bb-loss"}
      />
      {excursions ? (
        <>
          <StatRow
            label="MAE"
            value={`${usd(excursions.maePerSet * qty)}${pctOf(excursions.maePerSet * qty)}`}
            cls="text-bb-loss"
          />
          <StatRow
            label="MFE"
            value={`${usd(excursions.mfePerSet * qty)}${pctOf(excursions.mfePerSet * qty)}`}
            cls="text-bb-profit"
          />
        </>
      ) : (
        <StatRow label="MAE/MFE" value="no bars in window" cls="text-bb-muted" />
      )}
      <StatRow label="TIME IN TRADE" value={holdLabel} />
      {plan.exit_premium != null && (
        <StatRow label="EXIT" value={plan.exit_premium.toFixed(2)} />
      )}
      <SpreadPaidRow plan={plan} qty={qty} />
    </div>
  );
}

/** Realized spread friction vs the submit-time fair value — the cost channel
 * the retail-options literature blames for ~60% of losses. Only renders when
 * fills were measured (entry and/or exit snapshots existed at submit). */
function SpreadPaidRow({ plan, qty }: { plan: Plan; qty: number }) {
  const eq = plan.exec_quality;
  if (!eq) return null;
  const legs = (["entry", "exit"] as const)
    .map((k) => ({ k, d: eq[k] }))
    .filter((x): x is { k: "entry" | "exit"; d: NonNullable<typeof x.d> } =>
      x.d?.cost !== undefined,
    );
  if (!legs.length) return null;
  const totalDollars = legs.reduce((a, x) => a + (x.d.cost ?? 0), 0) * 100 * qty;
  const capture = legs
    .map((x) => x.d.spread_capture)
    .filter((c): c is number => c !== undefined);
  const captureLabel = capture.length
    ? ` · capt ${Math.round((capture.reduce((a, c) => a + c, 0) / capture.length) * 100)}%`
    : "";
  return (
    <StatRow
      label="SPREAD PAID"
      value={`${usd(-totalDollars)}${captureLabel}`}
      cls={totalDollars <= 0 ? "text-bb-profit" : "text-bb-orange"}
    />
  );
}

/** "What is the enforcer doing for THIS position right now" — the live
 * conditionals of the bracket, shown while viewing a position on the chart.
 * Polls system state (monitor/health) and the plan's lifecycle journal. */
function EnforcerBlock({ planId }: { planId: string }) {
  const plan = useAccountStore((s) => s.positions.find((p) => p.id === planId) ?? null);
  const [sys, setSys] = useState<SystemState | null>(null);
  const [events, setEvents] = useState<PlanEvent[]>([]);

  useEffect(() => {
    let live = true;
    const tick = () => {
      getSystemState().then((s) => live && setSys(s)).catch(() => {});
      getPlanEvents(planId).then((e) => live && setEvents(e.slice(0, 3))).catch(() => {});
    };
    tick();
    const id = window.setInterval(tick, 5000);
    return () => {
      live = false;
      window.clearInterval(id);
    };
  }, [planId]);

  if (!plan) return null;
  const monitored = sys?.enforcer.monitored_plan_ids.includes(planId) ?? null;
  const health = sys?.enforcer.monitors_without_mid?.[planId];
  const mark = plan.mark;
  const span = Math.abs(plan.tp_premium - plan.sl_premium) || 1;
  const slDist = mark != null ? ((mark - plan.sl_premium) / span) * 100 : null;

  return (
    <div
      className="pointer-events-none flex flex-col gap-0.5 border border-bb-amber/50 bg-black/80 px-1.5 py-1"
      title="Live bracket state: exactly what the exit enforcer is watching and will do for this position"
    >
      <div className="flex justify-between gap-2">
        <span className="tracking-widest text-bb-amber">ENFORCER</span>
        <span
          className={
            monitored === null
              ? "text-bb-muted"
              : monitored && !health
                ? "text-bb-profit"
                : "text-bb-orange"
          }
        >
          {monitored === null ? "…" : !monitored ? "⚠ NO MONITOR" : health ? `⚠ ${health}` : "● WATCHING"}
        </span>
      </div>
      <StatRow
        label="MARK vs SL/TP"
        value={
          mark != null
            ? `${mark.toFixed(2)} in [${plan.sl_premium.toFixed(2)} … ${plan.tp_premium.toFixed(2)}]`
            : "—"
        }
        cls={slDist != null && slDist < 25 ? "text-bb-orange" : "text-white"}
      />
      {slDist != null && (
        <StatRow
          label="TO STOP"
          value={`${Math.max(slDist, 0).toFixed(0)}% of bracket`}
          cls={slDist < 25 ? "text-bb-loss" : "text-bb-muted"}
        />
      )}
      <StatRow
        label="TP"
        value={plan.tp_order_id ? "RESTING @ BROKER" : "software trigger"}
        cls={plan.tp_order_id ? "text-bb-profit" : "text-bb-muted"}
      />
      <StatRow
        label="SL"
        value="µprice→KF fair value + dwell → ladder −2%→−6%→MKT"
        cls="text-bb-muted"
      />
      <StatRow label="TIME STOP" value={etCountdown(plan.time_stop_utc)} cls="text-bb-orange" />
      {events.map((e, i) => (
        <div key={i} className="truncate text-[9px] leading-tight text-bb-muted">
          {e.ts ? e.ts.slice(11, 19) : "—"} {e.event}
          {e.target ? `→${e.target}` : ""} {e.applied ? "" : "(dropped)"}
        </div>
      ))}
    </div>
  );
}

/** Theta-sell block: delta-targeted templates + credit-selling metrics.
 * Templates resolve short strikes by |delta| from the LIVE chain.
 * Exported so the mobile layout can host it in a tab below the chart. */
export function ThetaBlock({ designer }: { designer: Designer }) {
  const applyThetaTemplate = useStrategyStore((s) => s.applyThetaTemplate);
  const chain = useStrategyStore((s) => s.chain);
  const [failed, setFailed] = useState<string | null>(null);

  const isCredit = designer.ready && designer.entry < 0;
  const credit = isCredit ? Math.abs(designer.entry) * 100 : 0;
  const structural = designer.legs ? structuralMaxLoss(designer.legs) : null;
  const maxLoss = structural !== null ? Math.abs(structural) * 100 : null;
  const width = maxLoss !== null ? maxLoss + credit : null; // wing width $
  const tau = designer.hoursToExpiry / TRADING_HOURS_PER_YEAR;
  // Per contract SET (like CREDIT/SET) — qty may still be 0 pre-sizing.
  const thetaDay = designer.legs
    ? designer.legs.reduce(
        (acc, leg) =>
          acc +
          leg.side * leg.qty * 100 * bsThetaPerDay(designer.spot, leg.strike, tau, leg.iv, leg.right),
        0,
      )
    : 0;
  // Expected move to expiry (1σ) vs the nearest short strike.
  const sigma = designer.probabilities?.sigmaUsed ?? 0;
  const em = designer.spot > 0 && sigma > 0 ? designer.spot * sigma * Math.sqrt(Math.max(tau, 0)) : 0;
  const shortStrikes = (designer.legs ?? []).filter((l) => l.side < 0).map((l) => l.strike);
  const minShortDist = shortStrikes.length
    ? Math.min(...shortStrikes.map((k) => Math.abs(k - designer.spot)))
    : null;
  const insideEm = minShortDist !== null && em > 0 && minShortDist < em;

  return (
    <div className="pointer-events-auto flex flex-col gap-0.5 border border-bb-border/60 bg-black/75 px-1.5 py-1">
      <div className="flex flex-wrap gap-px">
        {THETA_TEMPLATES.map((t) => (
          <button
            key={t.id}
            disabled={!chain}
            title={t.title}
            onClick={() => setFailed(applyThetaTemplate(t.id) ? null : t.label)}
            className="border border-bb-border px-1 py-0.5 text-bb-muted hover:text-bb-amber disabled:opacity-30"
          >
            {t.label}
          </button>
        ))}
      </div>
      {failed && (
        <span className="text-bb-loss">couldn't resolve {failed} — no OTM deltas in chain</span>
      )}
      {isCredit ? (
        <>
          <StatRow label="CREDIT/SET" value={`$${credit.toFixed(0)}`} cls="text-bb-profit" />
          {width !== null && width > 0 && (
            <StatRow
              label="CREDIT/WIDTH"
              value={`${((credit / width) * 100).toFixed(0)}%`}
              cls={credit / width >= 0.25 ? "text-bb-profit" : "text-bb-orange"}
            />
          )}
          <StatRow
            label="THETA/DAY/SET"
            value={usd(thetaDay)}
            cls={thetaDay >= 0 ? "text-bb-profit" : "text-bb-loss"}
          />
          {em > 0 && (
            <StatRow label="EXP MOVE ±" value={`$${em.toFixed(2)}`} cls="text-bb-amber" />
          )}
          {insideEm && (
            <span className="leading-tight text-bb-orange" title="A short strike sits closer than one expected move (1σ to expiry) — high touch probability">
              ⚠ SHORT STRIKE INSIDE EXPECTED MOVE
            </span>
          )}
        </>
      ) : (
        <span className="text-bb-muted">pick a template — current position is not net credit</span>
      )}
    </div>
  );
}

export function ChartHud({
  designer,
  barsRef,
  variant = "full",
}: {
  designer: Designer;
  barsRef: React.RefObject<Bars>;
  /** "chips": toggles + one legend line only — the phone layout hosts the
   * MC/theta data in tabs below the chart instead of floating over it.
   * "sidebar": same content as "full" but rendered as a permanent static
   * column (desktop hosts it left of the chart so it never occludes the
   * canvas or its drag handles). */
  variant?: "full" | "chips" | "sidebar";
}) {
  const indicators = useTradingStore((s) => s.indicators);
  const toggleIndicator = useTradingStore((s) => s.toggleIndicator);
  const viewingPlanId = useUiStore((s) => s.viewingPlanId);
  const viewedHistorical = useUiStore((s) => s.viewedHistorical);
  const tf = useTradingStore((s) => s.tf);
  const volShift = useStrategyStore((s) => s.volShift);
  const setVolShift = useStrategyStore((s) => s.setVolShift);
  const skewBeta = useStrategyStore((s) => s.skewBeta);
  const setSkewBeta = useStrategyStore((s) => s.setSkewBeta);
  const [mc, setMc] = useState<McResult | null>(null);
  const mcRef = useRef<McResult | null>(null);
  mcRef.current = mc;

  const mcInputs: McInputs | null = useMemo(() => {
    if (variant === "chips" || !indicators.sim || !designer.ready || !designer.legs) return null;
    return {
      legs: designer.legs,
      entry: designer.entry,
      tpPremium: designer.tpPremium,
      slPremium: designer.slPremium,
      hoursToExpiry: designer.hoursToExpiry,
      timeStopHours: designer.timeStopHours,
      spot: designer.spot,
      smiles: designer.smiles,
      volShift,
      skewBeta,
      frictionPerSet: designer.frictionPerSet,
    };
  }, [designer, volShift, skewBeta, indicators.sim, variant]);

  useMonteCarlo(mcInputs, setMc);

  // Bars context (ATR / RV) — read from the render ref; designer updates
  // arrive often enough that this stays fresh without its own ticker.
  const bars = barsRef.current;
  const tfMinutes = TF_MS[tf] / 60000;
  const atr = bars && bars.n ? computeAtr(bars) : 0;
  const rv = bars && bars.n ? realizedVolAnnualized(bars, 30, tfMinutes) : 0;
  const iv = designer.legs ? (designer.probabilities?.sigmaUsed ?? 0) : 0;
  const ivRv = iv > 0 && rv > 0 ? iv - rv : null;

  const p = designer.probabilities;

  const rootCls =
    variant === "sidebar"
      ? "flex h-full w-52 shrink-0 flex-col gap-1 overflow-y-auto bg-bb-panel p-1.5 text-[10px]"
      : "pointer-events-none absolute left-1.5 top-1.5 z-10 flex w-56 flex-col gap-1 text-[10px]";

  return (
    <div className={rootCls}>
      <div className="pointer-events-auto flex flex-wrap gap-px">
        {TOGGLES.map(({ key, label, title }) => (
          <button
            key={key}
            onClick={() => toggleIndicator(key)}
            title={title}
            className={
              "border border-bb-border px-1.5 py-0.5 " +
              (indicators[key]
                ? "bg-bb-hover font-semibold text-bb-amber"
                : "bg-black/70 text-bb-muted hover:text-bb-amber")
            }
          >
            {label}
          </button>
        ))}
        <label
          className="ml-1 flex items-center gap-0.5 border border-bb-border bg-black/70 px-1 text-bb-muted"
          title="IV shock: parallel scenario vol shift for surface, contours, and sim"
        >
          IV
          <input
            data-numeric
            type="number"
            step={5}
            min={-50}
            max={50}
            className="w-9 bg-transparent text-right text-bb-amber outline-none"
            value={Math.round(volShift * 100)}
            onChange={(e) => setVolShift(Number(e.target.value) / 100)}
            aria-label="IV shock percent"
          />
          %
        </label>
        <label
          className="flex items-center gap-0.5 border border-bb-border bg-black/70 px-1 text-bb-muted"
          title="Skew beta: chain-derived directional vol response (selloff => vols up)"
        >
          <input
            type="checkbox"
            checked={skewBeta}
            onChange={(e) => setSkewBeta(e.target.checked)}
            aria-label="Apply skew beta"
          />
          β
        </label>
      </div>

      <div className="pointer-events-none flex items-center gap-1 bg-black/70 px-1.5 py-0.5 text-bb-muted">
        {ivRv !== null && (
          <span
            className={
              "h-2.5 w-1 " + (ivRv > 0.03 ? "bg-bb-orange" : ivRv < -0.03 ? "bg-bb-profit" : "bg-bb-muted")
            }
            title="IV−RV: orange = premium rich (hurts buyers), green = cheap"
          />
        )}
        <span data-numeric>
          ATR {atr ? atr.toFixed(2) : "—"} · RV {rv ? (rv * 100).toFixed(1) : "—"}%
          {iv > 0 && ` · IV ${(iv * 100).toFixed(1)}%`}
          {ivRv !== null && ` · ${ivRv >= 0 ? "+" : ""}${(ivRv * 100).toFixed(1)}pt`}
        </span>
      </div>

      {viewingPlanId && viewedHistorical?.id === viewingPlanId ? (
        <HistoricalBlock plan={viewedHistorical} barsRef={barsRef} />
      ) : (
        viewingPlanId && <EnforcerBlock planId={viewingPlanId} />
      )}

      {variant !== "chips" && indicators.theta && <ThetaBlock designer={designer} />}

      {variant !== "chips" && indicators.sim && designer.ready && p && (
        <div
          className="pointer-events-none flex flex-col gap-0.5 border border-bb-border/60 bg-black/75 px-1.5 py-1"
          title="Monte Carlo: 2000 paths with the enforcer's exact exit rules, net of spread friction. Analytic rows: risk-neutral GBM."
        >
          {mc ? (
            <>
              <StatRow
                label="MC EV/SET"
                value={usd(mc.evPerSet)}
                cls={mc.evPerSet >= 0 ? "text-bb-profit" : "text-bb-loss"}
              />
              <StatRow label="WIN" value={pct(mc.winRate)} />
              <StatRow
                label="EXITS"
                value={`TP ${Math.round(mc.pTp * 100)}·SL ${Math.round(mc.pSl * 100)}·T ${Math.round((mc.pTime + mc.pExpiry) * 100)}`}
              />
              <StatRow label="P5·P50·P95" value={`${usd(mc.p5)}·${usd(mc.p50)}·${usd(mc.p95)}`} />
              <StatRow
                label="AVG TIME"
                value={
                  mc.avgMinutesInTrade >= 90
                    ? `${(mc.avgMinutesInTrade / 60).toFixed(1)}h`
                    : `${Math.round(mc.avgMinutesInTrade)}m`
                }
              />
            </>
          ) : (
            <StatRow label="MC" value="simulating…" cls="text-bb-muted" />
          )}
          <StatRow label="P(PROFIT)" value={pct(p.pProfitExpiry)} />
          <StatRow
            label="TOUCH TP/SL"
            value={`${pct(p.pTouchTp)} / ${pct(p.pTouchSl)}`}
          />
          <StatRow label="R:R" value={p.rr === null ? "—" : `${p.rr.toFixed(2)}:1`} />
        </div>
      )}
    </div>
  );
}
