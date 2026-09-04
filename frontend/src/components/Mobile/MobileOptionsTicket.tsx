/**
 * Phone options ticket — one scrolling sheet, top to bottom in the order a
 * trader decides: STRUCTURE (preset chips, only what the account can
 * place) → EXPIRY → LEGS (strike steppers, tap the strike for the ladder)
 * → EXITS (TP/SL steppers with the vol-scaled suggestion, time stop) →
 * SIZE → the numbers that matter → one big button and a confirm strip.
 * Same designer, same store, same order payload as the desktop panels; the
 * chart above still draws the payoff and drags the strikes.
 */

import { useMemo, useState } from "react";
import { suggestSlPctFromUnderlying } from "../../lib/analytics";
import { apiError, postOrder } from "../../lib/api";
import { playCue } from "../../lib/audio";
import { legsAllowed, presetAllowed, useCapabilities } from "../../lib/capabilities";
import { pct as fmtPct, usd } from "../../lib/format";
import type { McResult } from "../../lib/mcSim";
import { nakedShortUnits } from "../../lib/optionsMath";
import { etTimePlusMinutes, liveLevel2Blocked, optionsOrderPayload } from "../../lib/orderPayload";
import { WorkSpreadToggle } from "../Panels/WorkSpreadToggle";
import type { Designer } from "../../lib/useDesigner";
import { useMonteCarlo, type McInputs } from "../../lib/useMonteCarlo";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
import {
  availableStrikes,
  findContract,
  STRATEGIES,
  useStrategyStore,
  type StrategyKind,
} from "../../store/strategyStore";
import { useTradingStore } from "../../store/tradingStore";

const GROUPS = ["DIRECTIONAL", "SPREADS", "VOLATILITY", "INCOME / NEUTRAL"] as const;

function dte(expiry: string): string {
  const et = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date());
  const days = Math.round((Date.parse(`${expiry}T00:00:00Z`) - Date.parse(`${et}T00:00:00Z`)) / 86_400_000);
  return days <= 0 ? "0DTE" : `${days}DTE`;
}

function Section({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="border-b border-bb-border">
      <div className="flex h-9 items-center justify-between px-3 text-[10px] tracking-widest text-bb-muted">
        <span>{title}</span>
        {right}
      </div>
      <div className="px-3 pb-3">{children}</div>
    </section>
  );
}

function Row({ label, value, cls, big }: { label: string; value: string; cls?: string; big?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-2 py-0.5">
      <span className="text-[12px] text-bb-muted">{label}</span>
      <span data-numeric className={(big ? "text-[16px] " : "text-[13px] ") + (cls ?? "text-white")}>
        {value}
      </span>
    </div>
  );
}

const stepBtn =
  "h-11 w-11 shrink-0 border border-bb-border text-[18px] leading-none text-bb-muted active:bg-bb-amber active:text-black disabled:opacity-30";

function Stepper({ label, value, display, onStep, accent, hint }: {
  label: string; value: number; display: string; onStep: (dir: 1 | -1) => void; accent?: string; hint?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-2 py-1">
      <span className="flex flex-col">
        <span className="text-[12px] text-bb-muted">{label}</span>
        {hint && <span data-numeric className="text-[11px] text-bb-muted">{hint}</span>}
      </span>
      <span className="flex items-center gap-1">
        <button className={stepBtn} onClick={() => onStep(-1)} aria-label={`decrease ${label}`}>−</button>
        <span data-numeric className={"w-20 text-center text-[16px] " + (accent ?? "text-white")} aria-label={`${label} ${value}`}>
          {display}
        </span>
        <button className={stepBtn} onClick={() => onStep(1)} aria-label={`increase ${label}`}>+</button>
      </span>
    </div>
  );
}

/** Strike ladder for one leg: listed strikes for its right, mid and delta,
 * ATM marked, current strike highlighted. Tap = pick. */
function StrikeLadder({ i, onClose }: { i: number; onClose: () => void }) {
  const chain = useStrategyStore((s) => s.chain);
  const expiry = useStrategyStore((s) => s.expiry);
  const strikes = useStrategyStore((s) => s.strikes);
  const rights = useStrategyStore((s) => s.rights);
  const setStrike = useStrategyStore((s) => s.setStrike);
  const spot = useTradingStore((s) => s.quote?.mid) || chain?.spot || 0;
  const snaps = availableStrikes(chain, expiry);
  const right = rights[i];
  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-bb-panel">
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-bb-border px-3">
        <span className="text-[12px] tracking-widest text-bb-amber">
          LEG {i + 1} · {right === "C" ? "CALLS" : "PUTS"} {expiry?.slice(5)}
        </span>
        <button className="h-11 px-3 text-[12px] text-bb-muted" onClick={onClose}>DONE</button>
      </div>
      <div className="grid grid-cols-4 border-b border-bb-border px-3 py-1 text-[10px] tracking-wider text-bb-muted">
        <span>STRIKE</span><span className="text-right">MID</span><span className="text-right">Δ</span><span className="text-right">IV</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {snaps.map((k) => {
          const c = chain && expiry ? findContract(chain, expiry, right, k) : null;
          const atm = Math.abs(k - spot) === Math.min(...snaps.map((x) => Math.abs(x - spot)));
          const on = strikes[i] === k;
          return (
            <button
              key={k}
              ref={(el) => { if (atm && el && !el.dataset.scrolled) { el.dataset.scrolled = "1"; el.scrollIntoView({ block: "center" }); } }}
              className={
                "grid h-12 w-full grid-cols-4 items-center border-b border-bb-border/40 px-3 text-[14px] " +
                (on ? "bg-bb-amber/15 text-bb-amber" : atm ? "bg-bb-hover/60 text-white" : "text-white")
              }
              onClick={() => { setStrike(i, k); onClose(); }}
            >
              <span data-numeric className="text-left font-semibold">{k}{atm ? <span className="ml-1 text-[9px] text-bb-muted">ATM</span> : ""}</span>
              <span data-numeric className="text-right">{c ? c.mid.toFixed(2) : "—"}</span>
              <span data-numeric className="text-right text-bb-muted">{c?.delta != null ? c.delta.toFixed(2) : "—"}</span>
              <span data-numeric className="text-right text-bb-muted">{c && c.iv > 0 ? `${Math.round(c.iv * 100)}%` : "—"}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function MobileOptionsTicket({ designer }: { designer: Designer }) {
  const symbol = useTradingStore((s) => s.symbol);
  const kind = useStrategyStore((s) => s.kind);
  const setKind = useStrategyStore((s) => s.setKind);
  const expiry = useStrategyStore((s) => s.expiry);
  const setExpiry = useStrategyStore((s) => s.setExpiry);
  const chain = useStrategyStore((s) => s.chain);
  const chainError = useStrategyStore((s) => s.chainError);
  const strikes = useStrategyStore((s) => s.strikes);
  const ratios = useStrategyStore((s) => s.ratios);
  const rights = useStrategyStore((s) => s.rights);
  const sides = useStrategyStore((s) => s.sides);
  const modified = useStrategyStore((s) => s.modified);
  const setStrike = useStrategyStore((s) => s.setStrike);
  const setRatio = useStrategyStore((s) => s.setRatio);
  const decRatio = useStrategyStore((s) => s.decRatio);
  const tpPct = useStrategyStore((s) => s.tpPct);
  const slPct = useStrategyStore((s) => s.slPct);
  const setTpPct = useStrategyStore((s) => s.setTpPct);
  const setSlPct = useStrategyStore((s) => s.setSlPct);
  const timeStopEt = useStrategyStore((s) => s.timeStopEt);
  const setTimeStopEt = useStrategyStore((s) => s.setTimeStopEt);
  const qty = useStrategyStore((s) => s.qty);
  const setQty = useStrategyStore((s) => s.setQty);
  const volShift = useStrategyStore((s) => s.volShift);
  const skewBeta = useStrategyStore((s) => s.skewBeta);
  const workSpread = useStrategyStore((s) => s.workSpread);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const caps = useCapabilities();
  const { live, loaded } = useTradingMode();

  const [ladder, setLadder] = useState<number | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [placed, setPlaced] = useState<string | null>(null);
  const [mc, setMc] = useState<McResult | null>(null);

  const mcInputs: McInputs | null = useMemo(() => {
    if (!designer.ready || !designer.legs) return null;
    return {
      legs: designer.legs, entry: designer.entry, tpPremium: designer.tpPremium, slPremium: designer.slPremium,
      hoursToExpiry: designer.hoursToExpiry, timeStopHours: designer.timeStopHours, spot: designer.spot,
      smiles: designer.smiles, volShift, skewBeta, frictionPerSet: designer.frictionPerSet,
    };
  }, [designer, volShift, skewBeta]);
  useMonteCarlo(mcInputs, setMc);

  const slSuggestion = useMemo(
    () => designer.ready && designer.legs
      ? suggestSlPctFromUnderlying(designer.legs, designer.entry, designer.spot, designer.hoursToExpiry, designer.timeStopHours)
      : null,
    [designer],
  );

  const snaps = availableStrikes(chain, expiry);
  const nakedCalls = designer.legs ? nakedShortUnits(designer.legs, "C") : 0;
  const l2Blocked = liveLevel2Blocked(live, designer);
  const levelBlocked = !legsAllowed(sides.map((s) => ({ side: s })), caps.optionsLevel);
  const canTrade =
    loaded && designer.ready && designer.qty > 0 && !designer.demo &&
    designer.instantExit === null && nakedCalls === 0 && !l2Blocked && !levelBlocked && caps.optionsAllowed;
  const p = designer.probabilities;
  const sizing = designer.sizing;
  const isCredit = designer.ready && designer.entry < 0;

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const plan = await postOrder(optionsOrderPayload({ designer, symbol, kind, modified, timeStopEt, workSpread }));
      playCue("submitted");
      setPlaced(plan.id);
      setConfirming(false);
      void refreshPositions();
    } catch (err) {
      playCue("rejected");
      setError(apiError(err));
    } finally {
      setSubmitting(false);
    }
  };

  const chip = (on: boolean, extra = "") =>
    "h-10 shrink-0 whitespace-nowrap border px-3 text-[12px] tracking-wider " +
    (on ? "border-bb-amber bg-bb-amber font-semibold text-black" : "border-bb-border text-bb-muted active:text-bb-amber") + extra;

  const allowedKinds = (Object.keys(STRATEGIES) as StrategyKind[]).filter((k) => presetAllowed(k, caps.optionsLevel));
  const tpPremium = designer.tpPremium;
  const slPremium = designer.slPremium;

  if (!caps.optionsAllowed && caps.loaded) {
    return (
      <div className="p-4 text-[13px] text-bb-muted">
        This account is not approved for options (level {caps.optionsLevel}). Set the level on the ACCOUNT tab if the broker has approved it.
      </div>
    );
  }

  return (
    <div className="flex flex-col pb-24">
      {ladder !== null && <StrikeLadder i={ladder} onClose={() => setLadder(null)} />}

      <Section title={`STRUCTURE${modified ? " · CUSTOM" : ""}`} right={
        <span className="text-[10px] text-bb-muted">
          {caps.spreadsAllowed ? "level 3" : "level 2 · long single-leg"}
        </span>
      }>
        <div className="flex flex-col gap-2">
          {GROUPS.map((group) => {
            const kinds = allowedKinds.filter((k) => STRATEGIES[k].group === group);
            if (!kinds.length) return null;
            return (
              <div key={group} className="chip-rail">
                {kinds.map((k) => (
                  <button key={k} className={chip(kind === k && !modified)} onClick={() => setKind(k)}>
                    {STRATEGIES[k].label}
                  </button>
                ))}
              </div>
            );
          })}
        </div>
      </Section>

      <Section title="EXPIRY">
        {chainError ? (
          <div className="text-[12px] text-bb-loss">chain: {chainError}</div>
        ) : (
          <div className="chip-rail">
            {(chain?.expirations ?? []).map((e) => (
              <button key={e} className={chip(expiry === e)} onClick={() => setExpiry(e)}>
                {e.slice(5)} <span className="ml-1 text-[10px] opacity-70">{dte(e)}</span>
              </button>
            ))}
            {!chain && <span className="text-[12px] text-bb-muted">loading chain…</span>}
          </div>
        )}
      </Section>

      <Section title="LEGS" right={<span className="text-[10px] text-bb-muted">tap a strike for the ladder · drag on chart</span>}>
        <div className="flex flex-col gap-2">
          {strikes.map((k, i) => {
            const c = chain && expiry && k !== undefined ? findContract(chain, expiry, rights[i], k) : null;
            const idx = snaps.indexOf(k);
            return (
              <div key={i} className="flex items-center gap-2">
                <span className={"w-12 shrink-0 text-[14px] font-semibold " + (sides[i] > 0 ? "text-bb-amber" : "text-bb-orange")}>
                  {sides[i] > 0 ? "+" : "−"}{ratios[i] ?? 1} {rights[i]}
                </span>
                <button className={stepBtn} disabled={idx <= 0} onClick={() => setStrike(i, snaps[idx - 1])} aria-label="lower strike">−</button>
                <button
                  className="flex h-11 min-w-0 flex-1 flex-col items-center justify-center border border-bb-border bg-black active:border-bb-amber"
                  onClick={() => setLadder(i)}
                >
                  <span data-numeric className="text-[16px] text-white">{k ?? "—"}</span>
                  <span data-numeric className="text-[10px] text-bb-muted">
                    {c ? `${c.mid.toFixed(2)}${c.delta != null ? ` · Δ${c.delta.toFixed(2)}` : ""}` : "no quote"}
                  </span>
                </button>
                <button className={stepBtn} disabled={idx < 0 || idx >= snaps.length - 1} onClick={() => setStrike(i, snaps[idx + 1])} aria-label="higher strike">+</button>
                {caps.spreadsAllowed && (
                  <span className="flex shrink-0 gap-px">
                    <button className="h-11 w-9 border border-bb-border text-[14px] text-bb-muted active:text-bb-amber" onClick={() => decRatio(i)} aria-label="fewer contracts">×−</button>
                    <button className="h-11 w-9 border border-bb-border text-[14px] text-bb-muted active:text-bb-amber" onClick={() => setRatio(i, (ratios[i] ?? 1) + 1)} aria-label="more contracts">×+</button>
                  </span>
                )}
              </div>
            );
          })}
          <Row label={`NET ${isCredit ? "CREDIT" : "DEBIT"}`} value={designer.ready ? Math.abs(designer.entry).toFixed(2) : "—"} cls={isCredit ? "text-bb-profit" : "text-bb-amber"} big />
        </div>
      </Section>

      <Section title="EXITS · ENFORCED SERVER-SIDE">
        <Stepper
          label="TAKE PROFIT"
          value={Math.round(tpPct * 100)}
          display={`+${Math.round(tpPct * 100)}%`}
          onStep={(d) => setTpPct(Math.max(0.05, Math.min(10, tpPct + d * 0.1)))}
          accent="text-bb-profit"
          hint={tpPremium != null ? `${tpPremium.toFixed(2)} · ≈ @${p?.tpBarrier?.toFixed(2) ?? "—"}` : undefined}
        />
        <Stepper
          label="STOP LOSS"
          value={Math.round(slPct * 100)}
          display={`−${Math.round(slPct * 100)}%`}
          onStep={(d) => setSlPct(Math.max(0.05, Math.min(3, slPct + d * 0.05)))}
          accent="text-bb-loss"
          hint={slPremium != null ? `${slPremium.toFixed(2)} · ≈ @${p?.slBarrier?.toFixed(2) ?? "—"}` : undefined}
        />
        {slSuggestion && (
          <button
            onClick={() => setSlPct(slSuggestion.slPct)}
            className={
              "mt-1 h-10 border px-3 text-[12px] " +
              (Math.abs(slPct - slSuggestion.slPct) < 0.026 ? "border-bb-profit text-bb-profit" : "border-bb-border text-bb-muted active:text-bb-amber")
            }
          >
            SUGGEST −{Math.round(slSuggestion.slPct * 100)}% (underlying 1.5σ ±{slSuggestion.movePct.toFixed(1)}% by the time stop)
          </button>
        )}
        <div className="mt-2 chip-rail">
          <span className="text-[12px] text-bb-muted">HOLD</span>
          {[5, 10, 20, 45].map((m) => (
            <button key={m} className={chip(timeStopEt === etTimePlusMinutes(m))} onClick={() => setTimeStopEt(etTimePlusMinutes(m))}>
              +{m}m
            </button>
          ))}
        </div>
        <div className="mt-2 flex items-center justify-between">
          <span className="text-[12px] text-bb-muted">TIME STOP (ET)</span>
          <input
            type="time"
            value={timeStopEt}
            onChange={(e) => e.target.value && setTimeStopEt(e.target.value)}
            className="h-11 border border-bb-border bg-black px-2 text-[16px] text-bb-orange outline-none focus:border-bb-amber"
            aria-label="Time stop (ET)"
          />
        </div>
        <div className="mt-2">
          <WorkSpreadToggle touch />
        </div>
      </Section>

      <Section title="SIZE">
        <Stepper
          label="CONTRACTS"
          value={designer.qty}
          display={String(designer.qty)}
          onStep={(d) => setQty(Math.max(0, Math.min(designer.autoQty, (qty > 0 ? qty : designer.autoQty) + d)))}
          accent="text-bb-amber"
          hint={`auto ${designer.autoQty} from max loss`}
        />
        {qty > 0 && qty !== designer.autoQty && (
          <button className="h-9 border border-bb-border px-3 text-[11px] text-bb-muted" onClick={() => setQty(0)}>
            BACK TO AUTO ({designer.autoQty})
          </button>
        )}
        {sizing && (
          <div className="mt-1">
            <Row big label={isCredit ? "CREDIT RECEIVED" : "COST"} value={`$${Math.abs(designer.entry * 100 * designer.qty).toFixed(0)}`} cls={isCredit ? "text-bb-profit" : "text-bb-amber"} />
            <Row big label="MAX LOSS @ SL" value={`-$${(sizing.perContractRisk * designer.qty).toFixed(0)}`} cls="text-bb-loss" />
            <Row label="ACCT RISK @ SL" value={designer.equity > 0 ? `${(((sizing.perContractRisk * designer.qty) / designer.equity) * 100).toFixed(2)}%` : "—"} cls="text-bb-orange" />
            <Row label="STRUCTURAL MAX" value={sizing.contracts > 0 ? `-$${((sizing.maxLossStructural / Math.max(sizing.contracts, 1)) * designer.qty).toFixed(0)}` : "—"} cls="text-bb-loss" />
            <Row label="EST. FRICTION" value={`-$${(designer.frictionPerSet * designer.qty).toFixed(0)}`} cls="text-bb-orange" />
            {sizing.reasons.map((r) => (
              <div key={r} className="text-[11px] text-bb-orange">⚠ {r}</div>
            ))}
          </div>
        )}
      </Section>

      {designer.ready && p && (
        <Section title="ODDS · MODEL + MONTE CARLO">
          <div className="grid grid-cols-2 gap-x-4">
            <Row label="MC EV / SET" value={mc ? usd(mc.evPerSet) : "…"} cls={mc && mc.evPerSet >= 0 ? "text-bb-profit" : "text-bb-loss"} />
            <Row label="WIN RATE" value={mc ? fmtPct(mc.winRate) : "…"} />
            <Row label="P(PROFIT)" value={fmtPct(p.pProfitExpiry)} />
            <Row label="R : R" value={p.rr === null ? "—" : `${p.rr.toFixed(2)} : 1`} />
            <Row label="TOUCH TP / SL" value={`${fmtPct(p.pTouchTp)} / ${fmtPct(p.pTouchSl)}`} />
            <Row label="EXITS TP·SL·T" value={mc ? `${Math.round(mc.pTp * 100)}·${Math.round(mc.pSl * 100)}·${Math.round((mc.pTime + mc.pExpiry) * 100)}` : "…"} />
            <Row label="P5 · P50 · P95" value={mc ? `${usd(mc.p5)} · ${usd(mc.p50)} · ${usd(mc.p95)}` : "…"} />
            <Row label="AVG TIME" value={mc ? (mc.avgMinutesInTrade >= 90 ? `${(mc.avgMinutesInTrade / 60).toFixed(1)}h` : `${Math.round(mc.avgMinutesInTrade)}m`) : "…"} />
          </div>
        </Section>
      )}

      <div className="flex flex-col gap-1 px-3 pt-3">
        {levelBlocked && (
          <div className="text-[12px] text-bb-loss">⚠ this structure needs options level 3 — the account is level {caps.optionsLevel}</div>
        )}
        {l2Blocked && !levelBlocked && (
          <div className="text-[12px] text-bb-loss">⚠ live account is options level 2 — long single-leg only</div>
        )}
        {designer.warnings.map((w) => (
          <div key={w} className="text-[11px] text-bb-orange">⚠ {w}</div>
        ))}
        {designer.instantExit && (
          <div className="text-[12px] text-bb-loss">⚠ {designer.instantExit.toUpperCase()} already breached at the current price — the order would exit instantly</div>
        )}
        {nakedCalls > 0 && (
          <div className="text-[12px] text-bb-loss">⚠ {nakedCalls} uncovered short call{nakedCalls > 1 ? "s" : ""} — Alpaca refuses naked calls; add a long wing</div>
        )}
        {error && !confirming && <div className="text-[12px] text-bb-loss">✗ {error}</div>}
        {placed && !error && <div className="text-[12px] text-bb-profit">✓ plan {placed.slice(0, 8)} submitted — enforcer armed</div>}
      </div>

      <div className="fixed inset-x-0 bottom-0 z-50 border-t border-bb-border bg-bb-panel p-2 pb-[max(env(safe-area-inset-bottom),8px)]">
        {!confirming ? (
          <button
            disabled={!canTrade || submitting}
            onClick={() => { setError(null); setPlaced(null); setConfirming(true); }}
            className={
              "h-14 w-full text-[15px] tracking-widest " +
              (canTrade
                ? live ? "bg-bb-loss font-semibold text-black active:bg-bb-orange" : "bg-bb-amber font-semibold text-black active:bg-bb-orange"
                : "border border-bb-border text-bb-muted")
            }
          >
            {canTrade
              ? `${isCredit ? "SELL" : "BUY"} ${designer.qty}× ${symbol} ${(modified ? "custom" : STRATEGIES[kind].label).toUpperCase()} (${live ? "LIVE" : "PAPER"})`
              : designer.ready ? "BLOCKED — SEE REASONS ABOVE" : "PRICE A STRUCTURE FIRST"}
          </button>
        ) : (
          <div className="flex flex-col gap-1">
            <div className={"border p-2 text-[12px] " + (live ? "border-bb-loss bg-bb-loss/10 text-bb-loss" : "border-bb-amber/60 bg-bb-amber/10 text-bb-amber")}>
              {live && <span className="font-semibold">LIVE ORDER — REAL MONEY · </span>}
              {symbol} {(modified ? "custom" : kind).replace(/_/g, " ").toUpperCase()} × {designer.qty} ·{" "}
              {isCredit ? "credit" : "debit"} {Math.abs(designer.entry).toFixed(2)} · TP {tpPremium?.toFixed(2)} · SL {slPremium?.toFixed(2)} · stop {timeStopEt} ET ·
              max loss ${sizing ? (sizing.perContractRisk * designer.qty).toFixed(0) : "—"}
            </div>
            {error && <div className="text-[12px] text-bb-loss">✗ {error}</div>}
            <div className="flex gap-1">
              <button disabled={submitting} onClick={() => void submit()} className="h-14 flex-[2] bg-bb-loss text-[14px] font-semibold tracking-widest text-black active:bg-bb-orange">
                {submitting ? "SUBMITTING…" : live ? "CONFIRM LIVE" : "CONFIRM"}
              </button>
              <button disabled={submitting} onClick={() => setConfirming(false)} className="h-14 flex-1 border border-bb-border text-[13px] text-bb-muted">
                CANCEL
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
