/* eslint-disable react-hooks/refs -- legacy chart pattern: props are mirrored
   into refs during render so rAF/canvas draw callbacks read fresh values
   without re-subscribing; predates the rule, behavior verified in prod. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { barsTable } from "../../lib/perspective";
import { focusFeed, onSnapshot } from "../../lib/barFeed";
import type { HeatmapResult } from "../../lib/heatmap.worker";
import {
  breakevensForBasis,
  makeScenarioModel,
  normPdf,
  positionEntryCost,
  positionIv,
  positionPlModel,
  positionValueModel,
  TRADING_HOURS_PER_YEAR,
  type Leg,
  type ScenarioModel,
  type Smiles,
} from "../../lib/optionsMath";
import { etMinutes, etOffsetMinutes } from "../../lib/et";
import { computeBollinger, computeEma, computeMacd, computeRsi, computeSma, computeVwap } from "../../lib/indicators";
import {
  adoptSeed,
  buildPositionView,
  buildUntrackedView,
  equityPositionOfPlan,
  equityPositionOfUntracked,
  marketIv,
  type EquityPosition,
  type PositionView,
} from "../../lib/positionView";
import { addTradingHours, shareExitDayIso } from "../../lib/tradingTime";
import { planDraftKey, untrackedDraftKey } from "../../lib/useExitDraft";
import { useHoldingDetail } from "../../lib/useHoldingDetail";
import { useExitDraftStore } from "../../store/exitDraftStore";
import type { Designer } from "../../lib/useDesigner";
import { ChartHud } from "./ChartHud";
import { publishScale, sharedBars } from "../../lib/chartShared";
import { useHeatmap } from "../../lib/useHeatmap";
import { tradingDateAhead } from "../../lib/equityMath";
import { deriveEquityPlan, type EquityPlan } from "../../lib/equityPlan";
import { useAccountStore } from "../../store/accountStore";
import { useEquityTicketStore } from "../../store/equityTicketStore";
import { TF_MS, freshSpot, isFastTf, quoteIsStale, useTradingStore, type IndicatorToggles } from "../../store/tradingStore";
import { useUiStore } from "../../store/uiStore";
import {
  availableStrikes,
  buildLegs,
  hoursToExpiry as calcHoursToExpiry,
  nearestStrike,
  smileFromChain,
  timeStopHoursFromEt,
  useStrategyStore,
} from "../../store/strategyStore";
import {
  computeLayout,
  extendDomain,
  fmtDayET,
  fmtPrice,
  fmtTimeET, fmtTimeSecET,
  indexToX,
  MIN_BARS_VISIBLE,
  priceDomain,
  priceToY,
  visibleRange,
  xToIndex,
  yToPrice,
  zoomX,
  zoomY,
  type Bars,
  type Layout,
  type ViewState,
} from "./scales";

const COLORS = {
  bg: "#000000",
  grid: "#1a1a1a",
  axisText: "#666666",
  up: "#00C853",
  down: "#FF1744",
  last: "#FFB000",
  crosshair: "#444444",
  volUp: "rgba(0,200,83,0.35)",
  volDown: "rgba(255,23,68,0.35)",
  strike: "#FFB000",
  strikeShort: "#FFA028",
  breakeven: "#FFFFFF",
  tp: "#00C853",
  sl: "#FF1744",
  expiry: "#2196F3",
  timeStop: "#FF6D00",
};

const EMPTY: Bars = {
  t: new Float64Array(0),
  o: new Float64Array(0),
  h: new Float64Array(0),
  l: new Float64Array(0),
  c: new Float64Array(0),
  v: new Float64Array(0),
  n: 0,
};

const RIGHT_PAD_FRAC = 0.08;
const STRIKE_HIT_PX = 6;
/** Inset of the strike-handle rail from the plot's right edge. */
const RAIL_INSET = 16;
const RAIL_HIT_PX = 9;
// Strike chips start right of the LegRail strip (44px, desktop) so the two
// leg affordances never overlap; on mobile the inset is just breathing room.
const CHIP_X = 48;
const CHIP_H = 18;
const CHIP_ZONE = 14; // width of the − / + click zones on a strike chip

type StrategyOverlay = {
  legs: (Leg & { symbol: string })[] | null;
  strikes: number[];
  strikeSides: number[]; // +1 long leg, -1 short leg (for coloring)
  strikeRights: ("C" | "P")[];
  ratios: number[];
  snapStrikes: number[];
  hoursToExpiry: number;
  timeStopHours: number;
  tpPremium: number | null;
  slPremium: number | null;
  tpPct: number;
  slPct: number;
  entry: number;
  sigma: number;
  spot: number;
  smiles: Smiles | null;
  volShift: number;
  skewBeta: boolean;
  /** Precomputed scenario model — the ONE pricing function shared by the
   * surface, contours, tooltip, and exit-drag inverse mapping. */
  model: ScenarioModel;
  /** Position view: overlay is a live plan, not the designer — no editing. */
  readOnly: boolean;
  /** Position view of an OPEN position: the TP / SL contours and the time
   * stop drag into the exit draft (adopt / move exits); strikes stay put. */
  exitsEditable: boolean;
  /** Position view: surface time anchor (entry timestamp, ms); null = now. */
  anchorMs: number | null;
  /** Position view: P/L basis (actual fill premium); null = leg net entry. */
  entryBasis: number | null;
  /** Model-free expiry breakevens against the active premium basis. */
  breakevens: number[];
  /** Closed-trade view: exit event (trading hours after entry), the actual
   * exit premium/reason and realized P/L — drives the EXIT marker. */
  exitHours: number | null;
  exitPremium: number | null;
  exitReason: string | null;
  realizedPnl: number | null;
  /** Chunked closing waves — one marker per wave when the close landed in
   * pieces (broker auto-liquidation). Empty = single exit marker only. */
  exitEvents: { hours: number; premium: number; qty: number }[];
};

/** Bar index the surface's t=0 column maps to: entry bar for a position
 * view (clamped into the loaded range), else the latest bar. */
function anchorIndexFor(bars: Bars, overlay: StrategyOverlay | null): number {
  return anchorIndexForMs(bars, overlay?.anchorMs ?? null);
}

function anchorIndexForMs(bars: Bars, anchorMs: number | null): number {
  const nowIdx = Math.max(bars.n - 1, 0);
  if (anchorMs === null || !bars.n) return nowIdx;
  const target = anchorMs;
  if (target <= bars.t[0]) return 0;
  if (target >= bars.t[nowIdx]) return nowIdx;
  let lo = 0;
  let hi = nowIdx;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (bars.t[mid] < target) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

type DragTarget =
  | { kind: "strike"; i: number }
  | { kind: "tp" }
  | { kind: "sl" }
  | { kind: "timestop" }
  // Equity ticket lines (share stop / target / exit day) — same drag
  // affordances the options designer has, writing to equityTicketStore.
  | { kind: "eqsl" }
  | { kind: "eqtp" }
  | { kind: "eqts" };

const RTH_MINUTES = 390;

/** Oscillator panes requested by the toggles (RSI, MACD stack under price). */
function oscCountOf(ind: IndicatorToggles): number {
  return (ind.rsi ? 1 : 0) + (ind.macd ? 1 : 0);
}

/** Price levels an equity plan wants on screen (bounded by extendDomain). */
function equityPositionLevels(pos: EquityPosition | null): number[] {
  if (!pos) return [];
  return [pos.entryPx, pos.sl, pos.tp].filter((v): v is number => v != null && v > 0);
}

function equityLevels(eq: EquityPlan | null): number[] {
  if (!eq || eq.price <= 0) return [];
  const levels = [Math.abs(eq.exits.entry), Math.abs(eq.exits.sl)];
  if (eq.exits.tp != null) levels.push(Math.abs(eq.exits.tp));
  return levels;
}

type ChipRect = { i: number; x: number; y: number; w: number; label: string };

function chipLabel(overlay: StrategyOverlay, i: number): string {
  const side = overlay.strikeSides[i] > 0 ? "+" : "−";
  const ratio = overlay.ratios[i] ?? 1;
  return `${side}${ratio} ${overlay.strikeRights[i] ?? ""}${fmtPrice(overlay.strikes[i])}`;
}

/** Chip rectangles for every visible strike, staggered so same-price legs
 * (straddles) stay individually clickable. Shared by render + hit tests. */
function computeChipRects(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  domain: [number, number],
  overlay: StrategyOverlay,
): ChipRect[] {
  const rects: ChipRect[] = [];
  overlay.strikes.forEach((strike, i) => {
    const y = priceToY(strike, domain, layout);
    if (y < 0 || y > layout.volTop) return;
    const label = chipLabel(overlay, i);
    const w = ctx.measureText(label).width + CHIP_ZONE * 2 + 8;
    let x = CHIP_X;
    for (const r of rects) {
      if (Math.abs(r.y - y) < CHIP_H && x < r.x + r.w + 4) x = r.x + r.w + 4;
    }
    rects.push({ i, x, y, w, label });
  });
  return rects;
}

export function CandlePane({
  designer,
  hudVariant,
}: {
  designer: Designer;
  /** "none": the host renders ChartHud itself (desktop left sidebar).
   * "readout": the phone's thin ATR/RV + enforcer overlay, no toggles. */
  hudVariant: "readout" | "none";
}) {
  const symbol = useTradingStore((s) => s.symbol);
  const tf = useTradingStore((s) => s.tf);
  const quote = useTradingStore((s) => s.quote);
  const assetMode = useTradingStore((s) => s.assetMode);

  const chain = useStrategyStore((s) => s.chain);
  const expiry = useStrategyStore((s) => s.expiry);
  const kind = useStrategyStore((s) => s.kind);
  const strikes = useStrategyStore((s) => s.strikes);
  const ratios = useStrategyStore((s) => s.ratios);
  const legRights = useStrategyStore((s) => s.rights);
  const legSides = useStrategyStore((s) => s.sides);
  const tpPct = useStrategyStore((s) => s.tpPct);
  const slPct = useStrategyStore((s) => s.slPct);
  const timeStopEt = useStrategyStore((s) => s.timeStopEt);
  const volShift = useStrategyStore((s) => s.volShift);
  const skewBeta = useStrategyStore((s) => s.skewBeta);
  const indicatorToggles = useTradingStore((s) => s.indicators);
  const setStrike = useStrategyStore((s) => s.setStrike);
  const setRatio = useStrategyStore((s) => s.setRatio);
  const decRatio = useStrategyStore((s) => s.decRatio);
  const setTpPct = useStrategyStore((s) => s.setTpPct);
  const setSlPct = useStrategyStore((s) => s.setSlPct);
  const setTimeStopEt = useStrategyStore((s) => s.setTimeStopEt);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const barsRef = useRef<Bars>(EMPTY);
  const viewRef = useRef<ViewState>({
    rightIndex: 0,
    barsVisible: 120,
    follow: true,
    yDomain: null,
  });
  const mouseRef = useRef<{ x: number; y: number } | null>(null);
  const dragRef = useRef<{
    startX: number;
    startY: number;
    startRight: number;
    startDomain: [number, number];
    vActive: boolean;
  } | null>(null);
  const axisDragRef = useRef<{ startY: number; domain: [number, number] } | null>(null);
  const dragTargetRef = useRef<DragTarget | null>(null);
  // Touch: every active pointer by id. Two of them make a pinch, which
  // owns the view until one lifts (the surviving finger does not pan —
  // that is how phones avoid the post-pinch jump).
  const pointersRef = useRef(new Map<number, { x: number; y: number }>());
  const pinchRef = useRef<{
    dist: number;
    midX: number;
    midY: number;
    barsVisible: number;
    rightIndex: number;
    domain: [number, number];
    axis: "x" | "y" | null;
  } | null>(null);
  const tapRef = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const sizedRef = useRef(false);
  const prevSymbolRef = useRef(symbol);
  const framedPlanRef = useRef<string | null>(null);
  const surfaceRef = useRef<HeatmapResult | null>(null);
  const overlayRef = useRef<StrategyOverlay | null>(null);
  // Visible y-domain, quantized to 25% steps of its own span: pans/zooms
  // only trigger a surface recompute when the view moves meaningfully.
  const [viewWindow, setViewWindow] = useState<[number, number] | null>(null);
  const viewWindowRef = useRef<[number, number] | null>(null);
  const indicatorsRef = useRef<IndicatorToggles>(indicatorToggles);
  indicatorsRef.current = indicatorToggles;
  // Tick-fresh display: the newest FRESH quote extends the forming bar so the
  // chart moves with every tick instead of once per bar close. Display-only —
  // stored bar data stays exactly what the feed delivered. In RTH-only mode
  // an extended-hours tick must NOT extend the last RTH candle (it would
  // falsify the session close), so the extension is gated to RTH minutes.
  const showEth = useTradingStore((s) => s.showEth);
  const showEthRef = useRef(showEth);
  showEthRef.current = showEth;
  const liveTickRef = useRef<{ mid: number; ts: number } | null>(null);
  liveTickRef.current = (() => {
    if (!quote || quote.mid <= 0 || quoteIsStale(quote)) return null;
    if (!showEth) {
      const minuteOfDay =
        ((quote.ts / 60_000 + etOffsetMinutes(quote.ts)) % 1440 + 1440) % 1440;
      if (minuteOfDay < RTH_START_MIN || minuteOfDay >= RTH_END_MIN) return null;
    }
    return { mid: quote.mid, ts: quote.ts };
  })();
  const rafRef = useRef(0);

  // ------------------------------------------------- strategy derivations

  const viewingPlanId = useUiStore((s) => s.viewingPlanId);
  const viewingUntracked = useUiStore((s) => s.viewingUntracked);
  const viewedHistorical = useUiStore((s) => s.viewedHistorical);
  const pnlMode = useUiStore((s) => s.pnlMode);
  const positions = useAccountStore((s) => s.positions);
  const untrackedRows = useAccountStore((s) => s.untracked);
  const account = useAccountStore((s) => s.account);
  const eqTicket = useEquityTicketStore();
  const viewingPlan = viewingPlanId
    ? positions.find((p) => p.id === viewingPlanId) ??
      (viewedHistorical?.id === viewingPlanId ? viewedHistorical : null)
    : null;
  const untrackedPos = !viewingPlan && viewingUntracked
    ? untrackedRows.find((u) => u.symbol === viewingUntracked) ?? null
    : null;
  const planClosed = viewingPlan ? ["closed", "cancelled", "rejected"].includes(viewingPlan.status) : false;
  const planEditable = viewingPlan ? ["partially_filled", "filled"].includes(viewingPlan.status) : false;
  // The holding's detail row (live IV, entry time) for the position in view.
  const detailSymbol = viewingPlan
    ? viewingPlan.legs.length === 1 ? viewingPlan.legs[0].symbol : null
    : untrackedPos?.symbol ?? null;
  const detail = useHoldingDetail(detailSymbol);
  const enteredAt = detail?.entered_at ?? null;
  // The contract's vol: the feed's snapshot IV when it has one (market
  // hours), else the vol implied by its quote mid / the broker's mark at
  // the underlying's spot — never a flat guess while a price exists.
  const detailIv = useMemo(() => {
    const snap = detail?.quote?.iv ?? null;
    if (snap && snap > 0) return snap;
    const occ = untrackedPos?.occ ?? null;
    const leg = viewingPlan && viewingPlan.legs.length === 1 ? viewingPlan.legs[0] : null;
    const strike = occ?.strike ?? leg?.strike ?? null;
    const right = occ?.right ?? leg?.right ?? null;
    const expiry = occ?.expiry ?? leg?.expiry ?? null;
    if (strike == null || right == null || expiry == null) return null;
    const price = detail?.quote?.mid ?? untrackedPos?.current_price ?? viewingPlan?.mark ?? null;
    const spot = detail?.underlying?.spot ?? (chain?.spot && chain.spot > 0 ? chain.spot : null) ?? (quote && quote.mid > 0 ? quote.mid : null);
    return marketIv({ price: price == null ? null : Math.abs(price), spot, strike, right, expiry });
  }, [detail, untrackedPos, viewingPlan, chain, quote]);
  const defaultSl = account?.risk?.default_sl_pct ?? 0.5;
  // The exit draft (chart drag <-> panel fields) for the position in view.
  const draftKey = viewingPlan ? planDraftKey(viewingPlan.id) : untrackedPos ? untrackedDraftKey(untrackedPos.symbol) : null;
  const storeKey = useExitDraftStore((s) => s.key);
  const storeDraft = useExitDraftStore((s) => s.draft);
  const draft = draftKey !== null && storeKey === draftKey ? storeDraft : null;

  const overlay: StrategyOverlay | null = useMemo(() => {
    // EQUITY mode: the options designer's payoff/TP/SL overlay is not this
    // ticket's model — plain candles (position views still draw, they carry
    // their own plan's rules).
    if (assetMode === "equity" && !viewingPlan && !untrackedPos?.occ) return null;
    // POSITION VIEW: chart inspects a live plan (legs + rules), read-only,
    // anchored at entry, P/L measured against the actual fill premium.
    const fromView = (positionView: PositionView, exitsEditable: boolean): StrategyOverlay => {
      const spot = freshSpot(quote, chain?.spot ?? 0);
      const live = pnlMode === "live";
      const entry = positionView.entryBasis;
      const shift = live ? volShift : 0;
      const beta = live ? skewBeta : false;
      return {
        legs: positionView.legs,
        strikes: positionView.strikes,
        strikeSides: positionView.strikeSides,
        strikeRights: positionView.strikeRights,
        ratios: positionView.ratios,
        snapStrikes: [],
        hoursToExpiry: positionView.hoursTotal,
        timeStopHours: positionView.timeStopHours,
        tpPremium: positionView.tpPremium,
        slPremium: positionView.slPremium,
        tpPct:
          positionView.tpPremium !== null && Math.abs(entry) >= 0.01
            ? (positionView.tpPremium - entry) / Math.abs(entry)
            : 0,
        slPct:
          positionView.slPremium !== null && Math.abs(entry) >= 0.01
            ? (entry - positionView.slPremium) / Math.abs(entry)
            : 0,
        entry,
        sigma: positionIv(positionView.legs),
        spot,
        smiles: positionView.smiles,
        volShift: shift,
        skewBeta: beta,
        model: makeScenarioModel(positionView.smiles, spot, shift, beta),
        readOnly: true,
        exitsEditable,
        anchorMs: positionView.anchorMs,
        entryBasis: entry,
        breakevens:
          spot > 0
            ? breakevensForBasis(positionView.legs, entry, spot * 0.7, spot * 1.3)
            : [],
        exitHours: positionView.exitHours,
        exitPremium: positionView.exitPremium,
        exitReason: positionView.exitReason,
        realizedPnl: positionView.realizedPnl,
        exitEvents: positionView.exitEvents,
      };
    };
    if (viewingPlan) {
      // The draft's exits draw while the plan is open (an edit in progress
      // is a line on the chart); a closed plan is a frozen fact.
      const positionView = buildPositionView(viewingPlan, chain, pnlMode, planClosed ? null : draft, detailIv);
      if (positionView) return fromView(positionView, !planClosed && planEditable);
    } else if (untrackedPos?.occ) {
      // UNTRACKED option: the broker row marked at its live IV, anchored at
      // the fill the broker's orders gave, exits from the adopt draft.
      const positionView = buildUntrackedView(untrackedPos, {
        iv: detailIv,
        enteredAt,
        exits: draft ?? adoptSeed(untrackedPos, defaultSl),
      });
      if (positionView) return fromView(positionView, true);
    }
    const legs = buildLegs({
      chain, expiry, kind, strikes, ratios, rights: legRights, sides: legSides,
    });
    if (!chain || !expiry) return null;
    const spot = freshSpot(quote, chain.spot);
    const hte = calcHoursToExpiry(expiry);
    const entry = legs ? positionEntryCost(legs) : 0;
    // Time stop: today at HH:MM ET, in trading hours from now.
    const timeStopHours = timeStopHoursFromEt(timeStopEt);
    const priced = legs !== null && Math.abs(entry) >= 0.01;
    return {
      legs,
      strikes,
      strikeSides: legSides,
      strikeRights: legRights,
      ratios: strikes.map((_, i) => ratios[i] ?? 1),
      snapStrikes: availableStrikes(chain, expiry),
      hoursToExpiry: hte,
      timeStopHours,
      // Signed premium axis: TP above entry, SL below — credit-safe.
      tpPremium: priced ? entry + Math.abs(entry) * tpPct : null,
      slPremium: priced ? entry - Math.abs(entry) * slPct : null,
      tpPct,
      slPct,
      entry,
      sigma: legs ? positionIv(legs) : 0,
      spot,
      smiles: smileFromChain(chain, expiry),
      volShift,
      skewBeta,
      model: makeScenarioModel(smileFromChain(chain, expiry), spot, volShift, skewBeta),
      readOnly: false,
      exitsEditable: false,
      anchorMs: null,
      entryBasis: null,
      breakevens:
        legs && spot > 0 ? breakevensForBasis(legs, entry, spot * 0.7, spot * 1.3) : [],
      exitHours: null,
      exitPremium: null,
      exitReason: null,
      realizedPnl: null,
      exitEvents: [],
    };
  }, [chain, expiry, kind, strikes, ratios, legRights, legSides, tpPct, slPct, timeStopEt, quote, volShift, skewBeta, viewingPlan, pnlMode, assetMode, untrackedPos, planClosed, planEditable, draft, detailIv, enteredAt, defaultSl]);

  overlayRef.current = overlay;

  // SHARE position view (a managed equity plan or an untracked stock row):
  // entry price + time, stop / target as prices, the exit day — from the
  // draft while open, so a drag or a typed price is a line immediately.
  const eqPosition: EquityPosition | null = useMemo(() => {
    if (viewingPlan) return equityPositionOfPlan(viewingPlan, planClosed ? null : draft);
    if (untrackedPos && !untrackedPos.occ) {
      return equityPositionOfUntracked(untrackedPos, { enteredAt, exits: draft ?? adoptSeed(untrackedPos, defaultSl) });
    }
    return null;
  }, [viewingPlan, planClosed, draft, untrackedPos, enteredAt, defaultSl]);
  const eqPosRef = useRef<EquityPosition | null>(null);
  eqPosRef.current = eqPosition;

  // EQUITY plan overlay: derived per draw (it reads the live bars for the
  // vol horizon), from a ref of the latest ticket/quote/account inputs.
  const eqInputsRef = useRef<{
    on: boolean;
    ticket: typeof eqTicket;
    quote: typeof quote;
    tf: typeof tf;
    account: typeof account;
  }>({ on: false, ticket: eqTicket, quote, tf, account });
  eqInputsRef.current = {
    // Position view (a plan or an untracked row) never shows the ticket's lines.
    on: assetMode === "equity" && !viewingPlan && !untrackedPos,
    ticket: eqTicket,
    quote,
    tf,
    account,
  };
  const eqPlanRef = useRef<EquityPlan | null>(null);
  const eqLevelsRef = useRef<number[]>([]);
  const computeEquityPlan = useCallback((): EquityPlan | null => {
    const inp = eqInputsRef.current;
    const plan = inp.on
      ? deriveEquityPlan({ ticket: inp.ticket, quote: inp.quote, bars: barsRef.current, tf: inp.tf, account: inp.account })
      : null;
    eqPlanRef.current = plan;
    eqLevelsRef.current = plan ? equityLevels(plan) : equityPositionLevels(eqPosRef.current);
    return plan;
  }, []);

  const draw = useCallback(() => {
    cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(() => {
      const canvas = canvasRef.current;
      const wrap = wrapRef.current;
      if (!canvas || !wrap) return;
      const dpr = window.devicePixelRatio || 1;
      const cssW = wrap.clientWidth;
      const cssH = wrap.clientHeight;
      if (!cssW || !cssH) return;
      if (canvas.width !== cssW * dpr || canvas.height !== cssH * dpr) {
        canvas.width = cssW * dpr;
        canvas.height = cssH * dpr;
        canvas.style.width = `${cssW}px`;
        canvas.style.height = `${cssH}px`;
      }
      const ctx = canvas.getContext("2d")!;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const layout = computeLayout(cssW, cssH, oscCountOf(indicatorsRef.current));
      const eqPlan = computeEquityPlan();
      if (!sizedRef.current) {
        sizedRef.current = true;
        // Phone-width plot: 120 bars at ~2.5px each is an unreadable smear.
        // Open around 4px per bar; pinch takes it from there.
        if (cssW < 640) {
          const v = viewRef.current;
          v.barsVisible = Math.max(
            MIN_BARS_VISIBLE,
            Math.min(v.barsVisible, Math.round(layout.plotW / 4)),
          );
          if (v.follow) v.rightIndex = Math.max(0, barsRef.current.n - 1) + v.barsVisible * RIGHT_PAD_FRAC;
        }
      }
      render(
        ctx,
        layout,
        barsRef.current,
        viewRef.current,
        mouseRef.current,
        overlayRef.current,
        surfaceRef.current,
        TF_MS[tf] / 60000,
        dragTargetRef.current,
        indicatorsRef.current,
        liveTickRef.current,
        showEthRef.current,
        eqPlan,
        eqPosRef.current,
      );
      // Feed HTML layers outside the canvas (sidebar HUD, leg rail).
      sharedBars.current = barsRef.current;
      const [lo, hi] = currentDomain(
        barsRef.current, viewRef.current, overlayRef.current, surfaceRef.current, eqLevelsRef.current,
      );
      publishScale({ lo, hi, volTopPx: layout.volTop });
      // Quantized view window for the heatmap grid (see useHeatmap).
      const step = Math.max((hi - lo) * 0.25, 1e-6);
      const qLo = Math.floor(lo / step) * step;
      const qHi = Math.ceil(hi / step) * step;
      const prev = viewWindowRef.current;
      if (!prev || Math.abs(prev[0] - qLo) > step * 0.5 || Math.abs(prev[1] - qHi) > step * 0.5) {
        viewWindowRef.current = [qLo, qHi];
        setViewWindow([qLo, qHi]);
      }
    });
  }, [tf, computeEquityPlan]);

  // The ticket's lines follow its inputs immediately (not the next tick).
  useEffect(() => {
    draw();
  }, [eqTicket, account, assetMode, draw]);
  // So does a share position's draft (typed price, dragged line).
  useEffect(() => {
    draw();
  }, [eqPosition, draw]);

  // Surface recompute (worker). The grid follows the QUANTIZED visible
  // window (dense vertical sampling at any zoom — see useHeatmap); pans
  // inside the same quantized window still remap without recompute.
  const surfaceInputs = useMemo(() => {
    if (!overlay || !overlay.legs) return null;
    return {
      legs: overlay.legs,
      hoursToExpiry: overlay.hoursToExpiry,
      spot: overlay.spot,
      tpPremium: overlay.tpPremium,
      slPremium: overlay.slPremium,
      smiles: overlay.smiles,
      volShift: overlay.volShift,
      skewBeta: overlay.skewBeta,
      entryOverride: overlay.entryBasis,
      viewLo: viewWindow ? viewWindow[0] : null,
      viewHi: viewWindow ? viewWindow[1] : null,
      // A 5s/15s/30s chart wants the surface to follow the tape: recompute
      // every 0.02% of spot (~$0.13 on SPY) instead of every 0.1%.
      spotQuant: isFastTf(tf) ? 0.0002 : 0.001,
    };
  }, [overlay, viewWindow, tf]);

  useHeatmap(surfaceInputs, (result) => {
    surfaceRef.current = result;
    draw();
  });

  useEffect(() => {
    if (!overlay?.legs) surfaceRef.current = null;
    draw();
  }, [overlay, draw, indicatorToggles]);

  // Every fresh quote repaints the forming bar (tick-level chart freshness).
  useEffect(() => {
    draw();
  }, [quote, draw]);

  // ------------------------------------------------------- data plumbing

  useEffect(() => {
    let disposed = false;
    let view: {
      delete(): Promise<void>;
      on_update(cb: () => void): void;
      to_columns(): Promise<Record<string, unknown[]>>;
    } | null = null;

    focusFeed(symbol, tf);

    // Ticker switch: the old symbol's manual y-scale and pan position are
    // meaningless on a different price level — snap back to auto-fit/follow
    // so the new symbol lands in view instead of off-screen.
    if (prevSymbolRef.current !== symbol) {
      prevSymbolRef.current = symbol;
      viewRef.current.yDomain = null;
      viewRef.current.follow = true;
    }

    async function pull() {
      if (!view || disposed) return;
      const cols = await view.to_columns();
      if (disposed) return;
      const t = (cols.t as number[] | undefined) ?? [];
      let idx = t.map((_, i) => i);
      // RTH-only default: extended-hours bars are dropped entirely, so days
      // sit contiguous on the index axis and future-time mapping is exactly
      // trading-time. The ETH toggle shows the full tape.
      if (!showEth && t.length) {
        const etOff = etOffsetMinutes(t[t.length - 1]);
        idx = idx.filter((i) => {
          const minuteOfDay = (((t[i] / 60_000 + etOff) % 1440) + 1440) % 1440;
          return minuteOfDay >= RTH_START_MIN && minuteOfDay < RTH_END_MIN;
        });
      }
      const n = idx.length;
      const pick = (col: unknown[]) => Float64Array.from(idx, (i) => col[i] as number);
      barsRef.current = n
        ? {
            t: pick(cols.t as number[]),
            o: pick(cols.o as number[]),
            h: pick(cols.h as number[]),
            l: pick(cols.l as number[]),
            c: pick(cols.c as number[]),
            v: pick(cols.v as number[]),
            n,
          }
        : EMPTY;
      const v = viewRef.current;
      // Historical replay: frame the holding window (entry -> exit, padded)
      // once per viewed plan as soon as bars cover it — a 1-minute scalp from
      // this morning must open ON SCREEN, not off the left edge.
      const hist = useUiStore.getState().viewedHistorical;
      if (hist && n && framedPlanRef.current !== hist.id) {
        const bars = barsRef.current;
        const anchorMs = Date.parse(hist.entered_at ?? hist.created_at);
        const exitMs = hist.exited_at ? Date.parse(hist.exited_at) : anchorMs;
        if (Number.isFinite(anchorMs) && anchorMs <= bars.t[n - 1]) {
          let aIdx = 0;
          let xIdx = n - 1;
          for (let i = 0; i < n; i++) {
            if (bars.t[i] <= anchorMs) aIdx = i;
            if (bars.t[i] <= exitMs) xIdx = i;
          }
          const span = Math.max(xIdx - aIdx, 1);
          v.barsVisible = Math.min(Math.max(span * 4, 60), 3000);
          v.rightIndex = xIdx + v.barsVisible * 0.3;
          v.follow = false;
          v.yDomain = null;
          framedPlanRef.current = hist.id;
        }
      } else if (!hist) {
        framedPlanRef.current = null;
      }
      if (v.follow) {
        v.rightIndex = Math.max(0, n - 1) + v.barsVisible * RIGHT_PAD_FRAC;
      }
      draw();
    }

    (async () => {
      const table = await barsTable(symbol, tf);
      if (disposed) return;
      view = (await table.view({
        columns: ["t", "o", "h", "l", "c", "v"],
        sort: [["t", "asc"]],
      })) as unknown as typeof view;
      view!.on_update(() => void pull());
      await pull();
    })();

    const offSnapshot = onSnapshot(() => void pull());
    return () => {
      disposed = true;
      offSnapshot();
      void view?.delete();
      barsRef.current = EMPTY;
    };
  }, [symbol, tf, draw, showEth]);

  useEffect(() => {
    const observer = new ResizeObserver(draw);
    if (wrapRef.current) observer.observe(wrapRef.current);
    return () => observer.disconnect();
  }, [draw]);

  // ---------------------------------------------------------- interactions

  const hitTestStrike = useCallback((y: number, tol = STRIKE_HIT_PX): number | null => {
    const overlayNow = overlayRef.current;
    const wrap = wrapRef.current;
    if (!overlayNow || overlayNow.readOnly || !overlayNow.strikes.length || !wrap) return null;
    const layout = computeLayout(wrap.clientWidth, wrap.clientHeight, oscCountOf(indicatorsRef.current));
    const domain = currentDomain(barsRef.current, viewRef.current, overlayNow, surfaceRef.current, eqLevelsRef.current);
    let best: number | null = null;
    let bestDist = tol + 1;
    overlayNow.strikes.forEach((strike, i) => {
      const dist = Math.abs(priceToY(strike, domain, layout) - y);
      if (dist < bestDist) {
        best = i;
        bestDist = dist;
      }
    });
    return best;
  }, []);

  /** Drag handles where each strike line crosses the vertical rail. */
  const hitTestRail = useCallback((x: number, y: number, tol = RAIL_HIT_PX): number | null => {
    const overlayNow = overlayRef.current;
    const wrap = wrapRef.current;
    if (!overlayNow || overlayNow.readOnly || !overlayNow.strikes.length || !wrap) return null;
    const layout = computeLayout(wrap.clientWidth, wrap.clientHeight, oscCountOf(indicatorsRef.current));
    if (Math.abs(x - (layout.plotW - RAIL_INSET)) > tol) return null;
    const domain = currentDomain(barsRef.current, viewRef.current, overlayNow, surfaceRef.current, eqLevelsRef.current);
    let best: number | null = null;
    let bestDist = tol + 1;
    overlayNow.strikes.forEach((strike, i) => {
      const dist = Math.abs(priceToY(strike, domain, layout) - y);
      if (dist < bestDist) {
        best = i;
        bestDist = dist;
      }
    });
    return best;
  }, []);

  /** Exit boundaries: TP/SL premium contours and the time-stop vertical. */
  const hitTestExit = useCallback((x: number, y: number, tol = 6): "tp" | "sl" | "timestop" | null => {
    const overlayNow = overlayRef.current;
    const surface = surfaceRef.current;
    const wrap = wrapRef.current;
    if (!overlayNow?.legs || (overlayNow.readOnly && !overlayNow.exitsEditable) || !surface || !wrap) return null;
    const layout = computeLayout(wrap.clientWidth, wrap.clientHeight, oscCountOf(indicatorsRef.current));
    const domain = currentDomain(barsRef.current, viewRef.current, overlayNow, surfaceRef.current, eqLevelsRef.current);
    const view = viewRef.current;
    const anchorIdx = anchorIndexFor(barsRef.current, overlayNow);
    const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
    if (y > layout.volTop) return null;
    const xTs = indexToX(futureIndex(overlayNow.timeStopHours, anchorIdx, tfMinutes), view, layout);
    if (Math.abs(x - xTs) <= tol) return "timestop";
    const x0 = indexToX(anchorIdx, view, layout);
    const xExp = indexToX(futureIndex(surface.hoursToExpiry, anchorIdx, tfMinutes), view, layout);
    if (x < x0 || x > xExp || xExp <= x0) return null;
    const ti = Math.max(
      0,
      Math.min(Math.round(((x - x0) / (xExp - x0)) * (surface.timeSteps - 1)), surface.timeSteps - 1),
    );
    const near = (line: Float64Array): boolean => {
      const s = line[ti];
      return isFinite(s) && Math.abs(priceToY(s, domain, layout) - y) <= tol;
    };
    if (near(surface.tpLine)) return "tp";
    if (near(surface.slLine)) return "sl";
    return null;
  }, []);

  /** Equity plan lines: stop / target horizontals, exit-day vertical. */
  const hitTestEquity = useCallback((x: number, y: number, tol = 6): "eqsl" | "eqtp" | "eqts" | null => {
    const wrap = wrapRef.current;
    const pos = eqPosRef.current;
    if (pos && wrap) {
      // Share position view: its lines drag into the exit draft.
      if (!pos.editable) return null;
      const layout = computeLayout(wrap.clientWidth, wrap.clientHeight, oscCountOf(indicatorsRef.current));
      if (y > layout.volTop) return null;
      const domain = currentDomain(barsRef.current, viewRef.current, null, null, eqLevelsRef.current);
      if (pos.timeStopHours != null) {
        const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
        const anchorIdx = anchorIndexForMs(barsRef.current, pos.anchorMs);
        const xTs = indexToX(futureIndex(pos.timeStopHours, anchorIdx, tfMinutes), viewRef.current, layout);
        if (Math.abs(x - xTs) <= tol) return "eqts";
      }
      if (pos.sl != null && Math.abs(priceToY(pos.sl, domain, layout) - y) <= tol) return "eqsl";
      if (pos.tp != null && Math.abs(priceToY(pos.tp, domain, layout) - y) <= tol) return "eqtp";
      return null;
    }
    const eq = eqPlanRef.current;
    if (!eq || eq.price <= 0 || !wrap) return null;
    const layout = computeLayout(wrap.clientWidth, wrap.clientHeight, oscCountOf(indicatorsRef.current));
    if (y > layout.volTop) return null;
    const domain = currentDomain(barsRef.current, viewRef.current, null, null, eqLevelsRef.current);
    const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
    const anchorIdx = Math.max(barsRef.current.n - 1, 0);
    const xTs = indexToX(anchorIdx + (eq.holdDays * RTH_MINUTES) / tfMinutes, viewRef.current, layout);
    if (Math.abs(x - xTs) <= tol) return "eqts";
    if (Math.abs(priceToY(Math.abs(eq.exits.sl), domain, layout) - y) <= tol) return "eqsl";
    if (eq.exits.tp != null && Math.abs(priceToY(Math.abs(eq.exits.tp), domain, layout) - y) <= tol) return "eqtp";
    return null;
  }, []);

  /** Strike chip zones: − / + edit the leg's contract ratio, middle drags. */
  const hitTestChip = useCallback(
    (x: number, y: number, padY = 0): { i: number; zone: "minus" | "plus" | "drag" } | null => {
      const overlayNow = overlayRef.current;
      const wrap = wrapRef.current;
      const canvas = canvasRef.current;
      if (!overlayNow || overlayNow.readOnly || !overlayNow.strikes.length || !wrap || !canvas) return null;
      const layout = computeLayout(wrap.clientWidth, wrap.clientHeight, oscCountOf(indicatorsRef.current));
      const domain = currentDomain(barsRef.current, viewRef.current, overlayNow, surfaceRef.current, eqLevelsRef.current);
      const ctx = canvas.getContext("2d")!;
      ctx.font = "11px 'SF Mono', Consolas, monospace";
      for (const rect of computeChipRects(ctx, layout, domain, overlayNow)) {
        if (x < rect.x || x > rect.x + rect.w || Math.abs(y - rect.y) > CHIP_H / 2 + padY) continue;
        if (x <= rect.x + CHIP_ZONE) return { i: rect.i, zone: "minus" };
        if (x >= rect.x + rect.w - CHIP_ZONE) return { i: rect.i, zone: "plus" };
        return { i: rect.i, zone: "drag" };
      }
      return null;
    },
    [],
  );

  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      const view = viewRef.current;
      const wrap = wrapRef.current!;
      const layout = computeLayout(wrap.clientWidth, wrap.clientHeight, oscCountOf(indicatorsRef.current));
      const rect = canvasRef.current!.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const factor = e.deltaY > 0 ? 1.15 : 1 / 1.15;
      if (x > layout.plotW && y <= layout.plotH) {
        // Wheel over the price axis: vertical scale around the cursor price.
        const domain = currentDomain(barsRef.current, view, overlayRef.current, surfaceRef.current, eqLevelsRef.current);
        zoomY(view, domain, layout, y, factor);
        draw();
        return;
      }
      zoomX(view, layout, x, factor);
      draw();
    },
    [draw],
  );

  const onMouseDown = useCallback(
    (e: React.PointerEvent) => {
      // Capture so touch drags keep streaming to the canvas even when the
      // finger wanders off it (also helps fast mouse drags).
      try {
        (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      } catch {
        /* unsupported pointer id (e.g. synthetic events) */
      }
      const rect = canvasRef.current!.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const wrap = wrapRef.current!;
      const layout = computeLayout(wrap.clientWidth, wrap.clientHeight, oscCountOf(indicatorsRef.current));
      const touch = e.pointerType === "touch";
      pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointersRef.current.size >= 2) {
        // Second finger: whatever the first started (pan, strike drag) is
        // over; the pair drives a pinch until one of them lifts.
        dragRef.current = null;
        axisDragRef.current = null;
        dragTargetRef.current = null;
        mouseRef.current = null;
        const [a, b] = [...pointersRef.current.values()];
        pinchRef.current = {
          dist: Math.hypot(b.x - a.x, b.y - a.y),
          midX: (a.x + b.x) / 2 - rect.left,
          midY: (a.y + b.y) / 2 - rect.top,
          barsVisible: viewRef.current.barsVisible,
          rightIndex: viewRef.current.rightIndex,
          domain: currentDomain(barsRef.current, viewRef.current, overlayRef.current, surfaceRef.current, eqLevelsRef.current),
          axis: null,
        };
        return;
      }
      tapRef.current = { x: e.clientX, y: e.clientY, moved: false };
      if (x > layout.plotW && y <= layout.plotH) {
        // Grab the price axis: vertical scale drag.
        axisDragRef.current = {
          startY: y,
          domain: currentDomain(barsRef.current, viewRef.current, overlayRef.current, surfaceRef.current, eqLevelsRef.current),
        };
        return;
      }
      // Finger targets are fatter than a mouse's: chips, the rail and the
      // exit contours get a wider hit band, and the bare strike LINE is not
      // a touch target at all — on a phone it hijacked ordinary pans.
      const chip = hitTestChip(x, y, touch ? 8 : 0);
      if (chip) {
        const ratios = overlayRef.current?.ratios ?? [];
        if (chip.zone === "minus") {
          decRatio(chip.i);
        } else if (chip.zone === "plus") {
          setRatio(chip.i, (ratios[chip.i] ?? 1) + 1);
        } else {
          dragTargetRef.current = { kind: "strike", i: chip.i };
        }
        return;
      }
      const rail = hitTestRail(x, y, touch ? RAIL_HIT_PX * 2 : RAIL_HIT_PX);
      if (rail !== null) {
        dragTargetRef.current = { kind: "strike", i: rail };
        return;
      }
      const exit = hitTestExit(x, y, touch ? 14 : 6);
      if (exit !== null) {
        dragTargetRef.current = { kind: exit };
        return;
      }
      const eqHit = hitTestEquity(x, y, touch ? 14 : 6);
      if (eqHit !== null) {
        dragTargetRef.current = { kind: eqHit };
        return;
      }
      const strikeIdx = touch ? null : hitTestStrike(y);
      if (strikeIdx !== null) {
        dragTargetRef.current = { kind: "strike", i: strikeIdx };
      } else {
        dragRef.current = {
          startX: e.clientX,
          startY: e.clientY,
          startRight: viewRef.current.rightIndex,
          startDomain: currentDomain(barsRef.current, viewRef.current, overlayRef.current, surfaceRef.current, eqLevelsRef.current),
          vActive: viewRef.current.yDomain !== null,
        };
      }
    },
    [hitTestChip, hitTestRail, hitTestExit, hitTestStrike, hitTestEquity, setRatio, decRatio],
  );

  const onMouseMove = useCallback(
    (e: React.PointerEvent) => {
      const rect = canvasRef.current!.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const wrap = wrapRef.current!;
      const layout = computeLayout(wrap.clientWidth, wrap.clientHeight, oscCountOf(indicatorsRef.current));

      if (pointersRef.current.has(e.pointerId)) {
        pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
      }
      const pinch = pinchRef.current;
      if (pinch && pointersRef.current.size >= 2) {
        // Pinch: the dominant spread direction picks the axis once, then the
        // gesture is stateless against its start (no drift on jittery
        // fingers). Horizontal = bars visible, vertical = price scale; the
        // midpoint's travel pans the time axis.
        const [a, b] = [...pointersRef.current.values()];
        const dist = Math.hypot(b.x - a.x, b.y - a.y);
        if (pinch.axis === null) {
          if (Math.abs(dist - pinch.dist) < 12) return;
          pinch.axis = Math.abs(b.x - a.x) >= Math.abs(b.y - a.y) ? "x" : "y";
        }
        const view = viewRef.current;
        const factor = pinch.dist / Math.max(dist, 1);
        if (pinch.axis === "x") {
          view.barsVisible = pinch.barsVisible;
          view.rightIndex = pinch.rightIndex;
          zoomX(view, layout, pinch.midX, factor);
          const midX = (a.x + b.x) / 2 - rect.left;
          view.rightIndex -= (midX - pinch.midX) / (layout.plotW / view.barsVisible);
        } else {
          zoomY(view, pinch.domain, layout, pinch.midY, factor);
        }
        draw();
        return;
      }
      const tap = tapRef.current;
      if (tap && !tap.moved && Math.hypot(e.clientX - tap.x, e.clientY - tap.y) > 6) tap.moved = true;
      mouseRef.current = { x, y };

      if (dragTargetRef.current !== null) {
        const target = dragTargetRef.current;
        const overlayNow = overlayRef.current;
        const eq = eqPlanRef.current;
        const eqPos = eqPosRef.current;
        if ((target.kind === "eqsl" || target.kind === "eqtp" || target.kind === "eqts") && eqPos) {
          // Share POSITION lines write the exit draft as absolute prices /
          // an absolute exit day (the plan convention: signed by side).
          const store = useExitDraftStore.getState();
          const domain = currentDomain(barsRef.current, viewRef.current, null, null, eqLevelsRef.current);
          if (target.kind === "eqts") {
            const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
            const anchorIdx = anchorIndexForMs(barsRef.current, eqPos.anchorMs);
            const idx = xToIndex(x, viewRef.current, layout);
            const hours = Math.max(0, hoursFromIndex(idx, anchorIdx, tfMinutes));
            const at = Math.max(Date.parse(addTradingHours(eqPos.anchorMs, hours)), Date.now());
            const iso = shareExitDayIso(at);
            if (iso !== store.draft.timeStopUtc) store.set({ timeStopUtc: iso });
          } else {
            const price = Math.round(yToPrice(y, domain, layout) * 100) / 100;
            if (price > 0) {
              const ok = target.kind === "eqsl"
                ? eqPos.side * (eqPos.entryPx - price) > 0
                : eqPos.side * (price - eqPos.entryPx) > 0;
              if (ok) store.set(target.kind === "eqsl" ? { sl: eqPos.side * price } : { tp: eqPos.side * price });
            }
          }
        } else if ((target.kind === "eqsl" || target.kind === "eqtp" || target.kind === "eqts") && eq) {
          // Share plan lines write straight into the ticket store, as %
          // of the entry price (the store's own units); the ticket, the
          // next draw and the order payload all follow from there.
          const store = useEquityTicketStore.getState();
          const domain = currentDomain(barsRef.current, viewRef.current, null, null, eqLevelsRef.current);
          const entryPx = Math.abs(eq.exits.entry);
          if (target.kind === "eqts") {
            const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
            const idx = xToIndex(x, viewRef.current, layout);
            const days = Math.round(((idx - Math.max(barsRef.current.n - 1, 0)) * tfMinutes) / RTH_MINUTES);
            const clamped = Math.max(1, Math.min(30, days));
            if (clamped !== eq.holdDays) store.setTimeStopDate(tradingDateAhead(clamped));
          } else if (entryPx > 0) {
            const price = yToPrice(y, domain, layout);
            const side = store.side;
            if (target.kind === "eqsl") {
              const pct = Math.round(((side * (entryPx - price)) / entryPx) * 100 * 2) / 2;
              if (pct >= 0.5 && pct !== store.slPct) store.setSlPct(pct);
            } else {
              const pct = Math.round(((side * (price - entryPx)) / entryPx) * 100 * 2) / 2;
              if (pct >= 1 && pct !== store.tpPct) store.setTarget(true, pct);
            }
          }
        } else if (overlayNow) {
          const domain = currentDomain(barsRef.current, viewRef.current, overlayNow, surfaceRef.current, eqLevelsRef.current);
          if (target.kind === "strike") {
            const price = yToPrice(y, domain, layout);
            const snaps = overlayNow.snapStrikes;
            if (snaps.length) {
              const snapped = nearestStrike(snaps, price);
              if (snapped !== overlayNow.strikes[target.i]) {
                setStrike(target.i, snapped);
              }
            }
          } else if (target.kind === "timestop" && overlayNow.exitsEditable && overlayNow.anchorMs !== null) {
            // Position view: the exit instant is absolute — trading hours
            // after the entry anchor, never in the past, never past expiry.
            const anchorIdx = anchorIndexFor(barsRef.current, overlayNow);
            const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
            const idx = xToIndex(x, viewRef.current, layout);
            const hours = Math.max(0, Math.min(hoursFromIndex(idx, anchorIdx, tfMinutes), overlayNow.hoursToExpiry));
            const at = Math.max(Date.parse(addTradingHours(overlayNow.anchorMs, hours)), Date.now() + 5 * 60_000);
            const iso = new Date(at).toISOString();
            const store = useExitDraftStore.getState();
            if (iso !== store.draft.timeStopUtc) store.set({ timeStopUtc: iso });
          } else if (target.kind === "timestop") {
            // Drag the force-exit time horizontally; snapped to 5 minutes,
            // clamped inside [now+5m, 15:55 ET] and before expiry.
            const anchorIdx = anchorIndexFor(barsRef.current, overlayNow);
            const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
            const idx = xToIndex(x, viewRef.current, layout);
            const hours = hoursFromIndex(idx, anchorIdx, tfMinutes);
            const nowMin = etMinutes();
            const maxMin = Math.min(15 * 60 + 55, nowMin + overlayNow.hoursToExpiry * 60);
            const targetMin = Math.max(
              nowMin + 5,
              Math.min(Math.round((nowMin + hours * 60) / 5) * 5, maxMin),
            );
            const hh = String(Math.floor(targetMin / 60)).padStart(2, "0");
            const mm = String(targetMin % 60).padStart(2, "0");
            setTimeStopEt(`${hh}:${mm}`);
          } else if (overlayNow.legs && Math.abs(overlayNow.entry) >= 0.01) {
            // TP/SL drag: invert price@time back to a premium level with the
            // SAME smile-aware pricing the contours use, then store as %.
            const surface = surfaceRef.current;
            const anchorIdx = anchorIndexFor(barsRef.current, overlayNow);
            const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
            const hte = surface?.hoursToExpiry ?? overlayNow.hoursToExpiry;
            const idx = xToIndex(x, viewRef.current, layout);
            const hours = Math.max(0, Math.min(hoursFromIndex(idx, anchorIdx, tfMinutes), hte));
            const tau = Math.max(hte - hours, 0) / TRADING_HOURS_PER_YEAR;
            const price = yToPrice(y, domain, layout);
            if (price > 0) {
              const premium = positionValueModel(overlayNow.legs, price, tau, overlayNow.model);
              const entry = overlayNow.entry;
              if (overlayNow.exitsEditable) {
                // Position view: the level IS the draft's premium. A target
                // stays on the profit side of entry, a stop on the loss side
                // (credit structures: the axis is signed, 0 is the ceiling).
                const p = Math.round(premium * 100) / 100;
                const store = useExitDraftStore.getState();
                if (target.kind === "tp") {
                  if (p > entry + 0.005 && (entry > 0 || p < 0)) store.set({ tp: p });
                } else if (p < entry - 0.005 && (entry < 0 || p > 0)) {
                  store.set({ sl: p });
                }
              } else if (target.kind === "tp") {
                setTpPct((premium - entry) / Math.abs(entry));
              } else {
                setSlPct((entry - premium) / Math.abs(entry));
              }
            }
          }
        }
      } else if (axisDragRef.current) {
        // Price-axis drag: stretch the vertical scale around the domain center.
        const { startY, domain } = axisDragRef.current;
        const factor = Math.exp((y - startY) / 200);
        const center = (domain[0] + domain[1]) / 2;
        const half = ((domain[1] - domain[0]) / 2) * factor;
        if (half > 1e-9) viewRef.current.yDomain = [center - half, center + half];
      } else if (dragRef.current) {
        const drag = dragRef.current;
        const barW = layout.plotW / viewRef.current.barsVisible;
        viewRef.current.rightIndex = drag.startRight - (e.clientX - drag.startX) / barW;
        viewRef.current.follow = false;
        // Vertical pan engages past a small threshold so ordinary horizontal
        // pans keep auto-fit; once engaged the price scale goes manual.
        const dy = e.clientY - drag.startY;
        if (!drag.vActive && Math.abs(dy) > 8) drag.vActive = true;
        if (drag.vActive) {
          const [lo, hi] = drag.startDomain;
          const shift = (dy * (hi - lo)) / layout.volTop;
          viewRef.current.yDomain = [lo + shift, hi + shift];
        }
      } else {
        const overAxis = x > layout.plotW && y <= layout.plotH;
        const chip = overAxis ? null : hitTestChip(x, y);
        const exit = chip || overAxis ? null : hitTestExit(x, y);
        const eqHit = overAxis ? null : hitTestEquity(x, y);
        const cursor = overAxis
          ? "ns-resize"
          : eqHit
            ? eqHit === "eqts" ? "ew-resize" : "ns-resize"
          : chip
            ? chip.zone === "drag"
              ? "ns-resize"
              : "pointer"
            : hitTestRail(x, y) !== null
              ? "ns-resize"
              : exit === "timestop"
                ? "ew-resize"
                : exit !== null
                  ? "ns-resize"
                  : hitTestStrike(y) !== null
                    ? "ns-resize"
                    : "crosshair";
        if (canvasRef.current!.style.cursor !== cursor) {
          canvasRef.current!.style.cursor = cursor;
        }
      }
      draw();
    },
    [draw, hitTestChip, hitTestRail, hitTestExit, hitTestStrike, hitTestEquity, setStrike, setTpPct, setSlPct, setTimeStopEt],
  );

  const endDrag = useCallback(() => {
    dragRef.current = null;
    axisDragRef.current = null;
    dragTargetRef.current = null;
  }, []);

  const onPointerUp = useCallback(
    (e: React.PointerEvent) => {
      const known = pointersRef.current.delete(e.pointerId);
      if (pointersRef.current.size < 2) pinchRef.current = null;
      const tap = tapRef.current;
      tapRef.current = null;
      endDrag();
      if (!known || e.pointerType !== "touch") return;
      // A tap parks the crosshair on that bar (inspect without a hover);
      // a pan or drag clears it so it never sticks mid-chart.
      if (!tap || tap.moved) mouseRef.current = null;
      draw();
    },
    [draw, endDrag],
  );

  const onMouseLeave = useCallback(
    (e: React.PointerEvent) => {
      // Touch "leaves" on every lift (capture release) — the parked
      // crosshair must survive that; onPointerUp already settled the state.
      if (e.pointerType === "touch") return;
      mouseRef.current = null;
      endDrag();
      draw();
    },
    [draw, endDrag],
  );

  const onDoubleClick = useCallback(() => {
    const view = viewRef.current;
    view.follow = true;
    view.yDomain = null; // back to auto-fit
    view.rightIndex = Math.max(0, barsRef.current.n - 1) + view.barsVisible * RIGHT_PAD_FRAC;
    draw();
  }, [draw]);

  return (
    <div ref={wrapRef} className="relative h-full w-full cursor-crosshair overflow-hidden">
      {hudVariant !== "none" && (
        <ChartHud designer={designer} barsRef={barsRef} variant={hudVariant} />
      )}
      {/* Pointer events (not mouse) so touch drives the same pan/drag
          interactions on phones; touch-none stops the page from scrolling
          while dragging the chart. */}
      <canvas
        ref={canvasRef}
        className="touch-none"
        onWheel={onWheel}
        onPointerDown={onMouseDown}
        onPointerMove={onMouseMove}
        onPointerUp={onPointerUp}
        // A cancelled pointer (touch gesture takeover, capture loss) never
        // sends pointerup; without these a strike drag stays armed and every
        // later hover keeps rewriting that leg — presets appeared "stuck".
        onPointerCancel={onPointerUp}
        onLostPointerCapture={onPointerUp}
        onPointerLeave={onMouseLeave}
        onDoubleClick={onDoubleClick}
      />
    </div>
  );
}

// ------------------------------------------------------------- rendering

/** Earliest-in-time finite value of a contour line (its "active now" level). */
function firstFinite(line: Float64Array): number | null {
  for (let i = 0; i < line.length; i++) if (isFinite(line[i])) return line[i];
  return null;
}

function currentDomain(
  bars: Bars,
  view: ViewState,
  overlay: StrategyOverlay | null,
  surface: HeatmapResult | null = null,
  extraLevels: number[] = [],
): [number, number] {
  // Manual vertical scale (axis wheel/drag or chart vertical pan) wins;
  // double-click restores auto-fit.
  if (view.yDomain) return view.yDomain;
  const base = priceDomain(bars, view);
  if (!overlay) return extraLevels.length ? extendDomain(base, extraLevels) : base;
  const levels = [...overlay.strikes];
  if (overlay.spot) levels.push(overlay.spot);
  // Auto-fit also keeps the TP/SL execution boundaries on screen, so editing
  // the % fields visibly moves the lines into view.
  if (surface) {
    const tp = firstFinite(surface.tpLine);
    const sl = firstFinite(surface.slLine);
    if (tp !== null) levels.push(tp);
    if (sl !== null) levels.push(sl);
  }
  return extendDomain(base, levels);
}

/** Bar-index offset from the surface anchor (entry bar in position view,
 * latest bar in designer view) in trading time. */
function futureIndex(hoursFromAnchor: number, anchorIdx: number, tfMinutes: number): number {
  return anchorIdx + ((hoursFromAnchor * 60) / tfMinutes) * FUTURE_SCALE;
}

/** Inverse of futureIndex: trading hours from the anchor at a bar index. */
function hoursFromIndex(idx: number, anchorIdx: number, tfMinutes: number): number {
  return ((idx - anchorIdx) * tfMinutes) / 60 / FUTURE_SCALE;
}

/** Bars per trading hour, relative to RTH: with extended-hours bars on the
 * tape a 6.5h trading session spans 16h of bars, so a trading-time offset
 * must stretch by that ratio or the expiry lands the same evening. Set by
 * render() from the ETH toggle; the hit tests and drags read it too. */
let FUTURE_SCALE = 1;
const ETH_SESSION_HOURS = 16;
const RTH_SESSION_HOURS = 6.5;

function render(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  mouse: { x: number; y: number } | null,
  overlay: StrategyOverlay | null,
  surface: HeatmapResult | null,
  tfMinutes: number,
  dragTarget: DragTarget | null,
  indicators: IndicatorToggles,
  liveTick: { mid: number; ts: number } | null = null,
  showEth = false,
  equity: EquityPlan | null = null,
  eqPos: EquityPosition | null = null,
) {
  FUTURE_SCALE = showEth ? ETH_SESSION_HOURS / RTH_SESSION_HOURS : 1;
  const draggingStrike = dragTarget?.kind === "strike" ? dragTarget.i : null;
  ctx.fillStyle = COLORS.bg;
  ctx.fillRect(0, 0, layout.width, layout.height);
  ctx.font = "11px 'SF Mono', Consolas, monospace";

  if (!bars.n) {
    ctx.fillStyle = COLORS.axisText;
    ctx.textAlign = "center";
    ctx.fillText("NO DATA — waiting for feed", layout.plotW / 2, layout.plotH / 2);
    return;
  }

  const domain = currentDomain(bars, view, overlay, surface, equityLevels(equity));
  const [first, last] = visibleRange(bars, view);
  const barW = layout.plotW / view.barsVisible;
  const bodyW = Math.max(1, Math.min(barW * 0.7, 14));

  // Live-bar extension: a fresh quote newer than the last bar's start moves
  // the forming candle between feed bar-closes. Display-only.
  const lastIdx = bars.n - 1;
  const live =
    liveTick && liveTick.ts >= bars.t[lastIdx]
      ? {
          c: liveTick.mid,
          h: Math.max(bars.h[lastIdx], liveTick.mid),
          l: Math.min(bars.l[lastIdx], liveTick.mid),
        }
      : null;

  const anchorIdx = anchorIndexFor(bars, overlay);
  const etOff = etOffsetMinutes(bars.t[bars.n - 1]);
  drawPriceGrid(ctx, layout, domain);
  if (showEth) drawSessionZones(ctx, layout, bars, view, first, last, etOff);
  drawSessionBoundaries(ctx, layout, bars, view, first, last, etOff);
  drawTimeAxis(ctx, layout, bars, view, first, last, overlay, tfMinutes, anchorIdx, showEth);

  // Heatmap first: background layer in the future region (HEAT toggle).
  if (overlay?.legs && surface && indicators.heat) {
    drawHeatmap(ctx, layout, bars, view, domain, overlay, surface, tfMinutes, dragTarget, anchorIdx);
  }

  drawVolume(ctx, layout, bars, view, first, last);

  // Everything priced on the y-scale is clipped to the price pane â€” a
  // manual scale (axis drag, vertical pan) puts bars off-domain, and those
  // must vanish at the pane edge, not paint over the volume and the axis.
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, layout.plotW, layout.volTop);
  ctx.clip();

  for (let i = first; i <= last; i++) {
    const x = indexToX(i, view, layout);
    if (x < -barW || x > layout.plotW + barW) continue;
    const isLive = live !== null && i === lastIdx;
    const c = isLive ? live.c : bars.c[i];
    const h = isLive ? live.h : bars.h[i];
    const l = isLive ? live.l : bars.l[i];
    const up = c >= bars.o[i];
    const color = up ? COLORS.up : COLORS.down;
    const yH = priceToY(h, domain, layout);
    const yL = priceToY(l, domain, layout);
    const yO = priceToY(bars.o[i], domain, layout);
    const yC = priceToY(c, domain, layout);
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, yH);
    ctx.lineTo(x, yL);
    ctx.stroke();
    const top = Math.min(yO, yC);
    ctx.fillRect(x - bodyW / 2, top, bodyW, Math.max(1, Math.abs(yC - yO)));
  }

  drawIndicators(ctx, layout, bars, view, domain, first, last, indicators);
  if (indicators.theta && overlay && overlay.sigma > 0 && overlay.spot > 0) {
    drawExpectedMove(ctx, layout, view, domain, bars, overlay, tfMinutes);
  }
  if (overlay) {
    drawStrikes(ctx, layout, domain, overlay, draggingStrike);
    drawRail(ctx, layout, domain, overlay, draggingStrike);
  }
  if (equity && equity.price > 0) {
    drawEquityPlan(ctx, layout, domain, view, bars, equity, tfMinutes, dragTarget);
  }
  if (eqPos) drawEquityPosition(ctx, layout, domain, view, bars, eqPos, tfMinutes, dragTarget);
  ctx.restore();
  if (layout.oscCount) drawOscillators(ctx, layout, bars, view, first, last, indicators);
  // Axis badges (TP/SL/BE, last price) live in the axis column: unclipped.
  if (equity && equity.price > 0) drawEquityBadges(ctx, layout, domain, equity);
  if (eqPos) drawEquityPositionBadges(ctx, layout, domain, eqPos);
  if (overlay?.legs && surface) drawExitLevels(ctx, layout, domain, surface, overlay);
  drawLastPrice(ctx, layout, bars, domain, live?.c);
  if (mouse && mouse.x <= layout.plotW && mouse.y <= layout.plotH) {
    drawCrosshair(ctx, layout, bars, view, domain, mouse);
    if (overlay?.legs && surface) {
      drawPlTooltip(ctx, layout, view, domain, overlay, surface, mouse, tfMinutes, anchorIdx);
    }
  }
}

/** Hover readout over the P/L surface: price, trading-time offset, and the
 * model P/L at that exact point (computed live, not sampled from the grid). */
function drawPlTooltip(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  view: ViewState,
  domain: [number, number],
  overlay: StrategyOverlay,
  surface: HeatmapResult,
  mouse: { x: number; y: number },
  tfMinutes: number,
  anchorIdx: number,
) {
  if (mouse.y > layout.volTop) return;
  const idx = xToIndex(mouse.x, view, layout);
  const hours = hoursFromIndex(idx, anchorIdx, tfMinutes);
  if (hours < 0 || hours > surface.hoursToExpiry) return;
  const price = yToPrice(mouse.y, domain, layout);
  if (price <= 0) return;
  const tau = Math.max(surface.hoursToExpiry - hours, 0) / TRADING_HOURS_PER_YEAR;
  const pl = positionPlModel(overlay.legs!, price, tau, overlay.model) * 100;
  const hLabel = hours >= 6.5 ? `+${(hours / 6.5).toFixed(1)}d` : `+${hours.toFixed(1)}h`;
  const pastStop = hours > overlay.timeStopHours + 1e-9;
  const txt =
    `S ${fmtPrice(price)}  ${hLabel}  P/L ${pl >= 0 ? "+" : "-"}$${Math.abs(pl).toFixed(0)}/set` +
    (pastStop ? "  · past time stop" : "");
  const w = ctx.measureText(txt).width + 12;
  let bx = mouse.x + 14;
  if (bx + w > layout.plotW) bx = mouse.x - w - 14;
  let by = mouse.y - 26;
  if (by < 0) by = mouse.y + 14;
  const color = pl >= 0 ? COLORS.up : COLORS.down;
  ctx.fillStyle = "rgba(0,0,0,0.92)";
  ctx.fillRect(bx, by, w, 18);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.strokeRect(bx, by, w, 18);
  ctx.fillStyle = color;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(txt, bx + 6, by + 9);
}

/** R-normalized diverging color, capped ±2R. Returns [r,g,b,a255]. */
function plColorRGBA(pl: number, risk: number): [number, number, number, number] {
  const r = Math.max(Math.min(pl / risk, 2), -2);
  if (r >= 0) {
    const t = r / 2;
    return [0, Math.round(120 + 80 * t), Math.round(50 + 30 * t), Math.round(255 * (0.28 + 0.3 * t))];
  }
  const t = -r / 2;
  return [
    Math.round(150 + 105 * t),
    Math.round(23 * (1 - t) + 10),
    Math.round(50 * (1 - t) + 18),
    Math.round(255 * (0.28 + 0.3 * t)),
  ];
}

/**
 * Rasterize the P/L grid once per (surface, risk) into an offscreen canvas —
 * one pixel per cell — then let drawImage's bilinear filtering stretch it into
 * a CONTINUOUS gradient. Re-renders on pan/zoom are a single blit.
 */
let surfaceImage: { surface: HeatmapResult; risk: number; canvas: HTMLCanvasElement } | null = null;

function surfaceToImage(surface: HeatmapResult, risk: number): HTMLCanvasElement {
  if (surfaceImage && surfaceImage.surface === surface && surfaceImage.risk === risk) {
    return surfaceImage.canvas;
  }
  const { priceSteps, timeSteps, grid } = surface;
  const canvas = document.createElement("canvas");
  canvas.width = timeSteps;
  canvas.height = priceSteps;
  const ctx = canvas.getContext("2d")!;
  const img = ctx.createImageData(timeSteps, priceSteps);
  for (let ti = 0; ti < timeSteps; ti++) {
    for (let pi = 0; pi < priceSteps; pi++) {
      // Image row 0 = top = highest price = last price index.
      const row = priceSteps - 1 - pi;
      const [r, g, b, a] = plColorRGBA(grid[ti * priceSteps + pi], risk);
      const off = (row * timeSteps + ti) * 4;
      img.data[off] = r;
      img.data[off + 1] = g;
      img.data[off + 2] = b;
      img.data[off + 3] = a;
    }
  }
  ctx.putImageData(img, 0, 0);
  surfaceImage = { surface, risk, canvas };
  return canvas;
}

function drawHeatmap(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  _bars: Bars,
  view: ViewState,
  domain: [number, number],
  overlay: StrategyOverlay,
  surface: HeatmapResult,
  tfMinutes: number,
  dragTarget: DragTarget | null,
  anchorIdx: number,
) {
  // Color scale: the loss at the stop, or with no stop the premium itself
  // (a long option's max loss) — never a flat $100.
  const risk = Math.max(
    overlay.slPremium !== null ? (overlay.entry - overlay.slPremium) * 100 : Math.abs(overlay.entry) * 100,
    1,
  );
  const x0 = indexToX(anchorIdx, view, layout);
  const xExpiry = indexToX(futureIndex(surface.hoursToExpiry, anchorIdx, tfMinutes), view, layout);
  if (xExpiry <= x0) return;

  const { priceLo, priceHi } = surface;
  const image = surfaceToImage(surface, risk);
  // Price axis is linear, so the grid maps affinely onto the plot. Half-cell
  // insets align pixel CENTERS with the grid's sample points.
  const halfPrice = (priceHi - priceLo) / (surface.priceSteps - 1) / 2;
  const yTop = priceToY(priceHi + halfPrice, domain, layout);
  const yBottom = priceToY(priceLo - halfPrice, domain, layout);
  ctx.save();
  ctx.beginPath();
  ctx.rect(Math.max(x0, 0), 0, layout.plotW - Math.max(x0, 0), layout.volTop);
  ctx.clip();
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(image, x0, yTop, xExpiry - x0, yBottom - yTop);
  ctx.restore();

  const { timeSteps } = surface;
  const colW = (xExpiry - x0) / timeSteps;
  const xTs = indexToX(
    futureIndex(Math.min(overlay.timeStopHours, surface.hoursToExpiry), anchorIdx, tfMinutes),
    view, layout,
  );

  // EXIT BOUNDARY: everything right of the time stop is a dead zone — the
  // enforcer force-closes there, so that P/L can never be realized.
  if (xTs < xExpiry) {
    ctx.fillStyle = "rgba(0,0,0,0.55)";
    ctx.fillRect(Math.max(xTs, 0), 0, Math.min(xExpiry, layout.plotW) - Math.max(xTs, 0), layout.volTop);
  }

  // Contours (drag the TP/SL lines to move the exits; % inputs stay synced).
  const lineAt = (line: Float64Array, color: string, dash: number[], width = 1.4) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash);
    ctx.beginPath();
    let started = false;
    let last: [number, number] | null = null;
    for (let ti = 0; ti < timeSteps; ti++) {
      const s = line[ti];
      if (!isFinite(s)) {
        started = false;
        continue;
      }
      const x = x0 + (ti + 0.5) * colW;
      const y = priceToY(s, domain, layout);
      if (x < 0 || x > layout.plotW || y < 0 || y > layout.volTop) {
        started = false;
        continue;
      }
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else ctx.lineTo(x, y);
      last = [x, y];
    }
    ctx.stroke();
    ctx.setLineDash([]);
    return last;
  };
  const label = (at: [number, number] | null, text: string, color: string) => {
    if (!at) return;
    ctx.font = "10px 'SF Mono', Consolas, monospace";
    const w = ctx.measureText(text).width + 8;
    const bx = Math.min(Math.max(at[0] - w - 4, 0), layout.plotW - w);
    const by = Math.min(Math.max(at[1] - 15, 0), layout.volTop - 13);
    ctx.fillStyle = "rgba(0,0,0,0.85)";
    ctx.fillRect(bx, by, w, 13);
    ctx.strokeStyle = color;
    ctx.lineWidth = 0.8;
    ctx.strokeRect(bx, by, w, 13);
    ctx.fillStyle = color;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, bx + w / 2, by + 6.5);
    ctx.font = "11px 'SF Mono', Consolas, monospace";
  };
  lineAt(surface.breakevenLine, COLORS.breakeven, [2, 3]);
  const tpEnd = lineAt(
    surface.tpLine, COLORS.tp, [6, 3], dragTarget?.kind === "tp" ? 2.6 : 1.6,
  );
  const slEnd = lineAt(
    surface.slLine, COLORS.sl, [6, 3], dragTarget?.kind === "sl" ? 2.6 : 1.6,
  );
  if (overlay.tpPremium !== null) {
    label(tpEnd, `TP ${overlay.tpPremium.toFixed(2)} (+${Math.round(overlay.tpPct * 100)}%) ⇕`, COLORS.tp);
  }
  if (overlay.slPremium !== null) {
    label(slEnd, `SL ${overlay.slPremium.toFixed(2)} (−${Math.round(overlay.slPct * 100)}%) ⇕`, COLORS.sl);
  }

  // Expiry + time-stop verticals.
  const vline = (x: number, color: string, text: string, width = 1) => {
    if (x < 0 || x > layout.plotW) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, layout.plotH);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.lineWidth = 1;
    ctx.fillStyle = color;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText(text, x, 4);
  };
  vline(xExpiry, COLORS.expiry, "EXPIRY");
  vline(
    xTs,
    COLORS.timeStop,
    overlay.readOnly && !overlay.exitsEditable ? "TIME STOP" : "⇔ TIME STOP",
    dragTarget?.kind === "timestop" ? 2.4 : 1.2,
  );
  if (overlay.readOnly) vline(x0, "#9c8cff", "ENTRY", 1.2);

  // Closed-trade EXIT markers. A close that landed in WAVES (broker
  // auto-liquidation chunks) draws one line + chip per wave at its own
  // time and premium; the summary chip (reason · net premium · realized
  // P/L) anchors at the LAST wave. Single-fill exits keep one marker.
  if (overlay.exitHours !== null) {
    const waves =
      overlay.exitEvents.length > 1
        ? overlay.exitEvents
        : [{
            hours: overlay.exitHours,
            premium: overlay.exitPremium ?? NaN,
            qty: NaN,
          }];
    const win = (overlay.realizedPnl ?? 0) >= 0;
    const color = win ? COLORS.tp : COLORS.sl;
    ctx.font = "11px 'SF Mono', Consolas, monospace";
    let chipY = 20;
    const chip = (text: string, xAt: number, emphasize: boolean) => {
      const w = ctx.measureText(text).width + 10;
      const cx = Math.min(Math.max(xAt - w / 2, 2), layout.plotW - w - 2);
      ctx.fillStyle = "rgba(0,0,0,0.88)";
      ctx.fillRect(cx, chipY, w, CHIP_H);
      ctx.globalAlpha = emphasize ? 1 : 0.75;
      ctx.strokeStyle = color;
      ctx.strokeRect(cx, chipY, w, CHIP_H);
      ctx.fillStyle = color;
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(text, cx + 5, chipY + CHIP_H / 2);
      ctx.globalAlpha = 1;
      chipY += CHIP_H + 4; // stack chips of near-coincident waves
    };
    for (let i = 0; i < waves.length; i++) {
      const wave = waves[i];
      const last = i === waves.length - 1;
      const xExit = indexToX(futureIndex(wave.hours, anchorIdx, tfMinutes), view, layout);
      if (xExit < 0 || xExit > layout.plotW) continue;
      ctx.strokeStyle = color;
      ctx.globalAlpha = last ? 1 : 0.65;
      ctx.lineWidth = last ? 1.6 : 1.2;
      ctx.beginPath();
      ctx.moveTo(xExit, 0);
      ctx.lineTo(xExit, layout.plotH);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.lineWidth = 1;
      if (waves.length > 1) {
        chip(`−${wave.qty}× @${wave.premium.toFixed(2)}`, xExit, false);
      }
      if (last) {
        const parts = [`EXIT ${(overlay.exitReason ?? "closed").toUpperCase()}`];
        if (overlay.exitPremium !== null) parts.push(`@${overlay.exitPremium.toFixed(2)}`);
        if (overlay.realizedPnl !== null) {
          parts.push(`${overlay.realizedPnl >= 0 ? "+" : "−"}$${Math.abs(overlay.realizedPnl).toFixed(0)}`);
        }
        chip(parts.join(" "), xExit, true);
      }
    }
  }

  // Terminal density strip along the expiry line (model-implied lognormal).
  if (overlay.sigma > 0 && overlay.spot > 0 && xExpiry > 0 && xExpiry <= layout.plotW) {
    const tau = surface.hoursToExpiry / TRADING_HOURS_PER_YEAR;
    const sq = overlay.sigma * Math.sqrt(tau);
    if (sq > 0) {
      const mu = Math.log(overlay.spot) + (0.05 - 0.5 * overlay.sigma ** 2) * tau;
      const maxW = 46;
      ctx.fillStyle = "rgba(255,176,0,0.20)";
      ctx.beginPath();
      ctx.moveTo(xExpiry, priceToY(domain[1], domain, layout));
      const samples = 80;
      let peak = 0;
      const densities: number[] = [];
      for (let i = 0; i <= samples; i++) {
        const p = domain[0] + ((domain[1] - domain[0]) * i) / samples;
        const d = p > 0 ? normPdf((Math.log(p) - mu) / sq) / (p * sq) : 0;
        densities.push(d);
        if (d > peak) peak = d;
      }
      for (let i = 0; i <= samples; i++) {
        const p = domain[0] + ((domain[1] - domain[0]) * i) / samples;
        const w = peak > 0 ? (densities[i] / peak) * maxW : 0;
        ctx.lineTo(xExpiry - w, priceToY(p, domain, layout));
      }
      ctx.lineTo(xExpiry, priceToY(domain[0], domain, layout));
      ctx.closePath();
      ctx.fill();
    }
  }
}

function drawStrikes(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  domain: [number, number],
  overlay: StrategyOverlay,
  draggingStrike: number | null,
) {
  // Lines first (chips render on top, staggered for same-strike legs).
  overlay.strikes.forEach((strike, i) => {
    const y = priceToY(strike, domain, layout);
    if (y < 0 || y > layout.volTop) return;
    const isShort = overlay.strikeSides[i] < 0;
    ctx.strokeStyle = isShort ? COLORS.strikeShort : COLORS.strike;
    ctx.lineWidth = draggingStrike === i ? 2.5 : 1.5;
    ctx.setLineDash(isShort ? [8, 4] : []);
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(layout.plotW, y);
    ctx.stroke();
    ctx.setLineDash([]);
  });
  // Chips: [−] ±ratio right strike [+] — click zones edit contracts, the
  // middle (or the line/rail handle) drags the strike. Read-only (position
  // view): plain labels, no edit affordances.
  for (const rect of computeChipRects(ctx, layout, domain, overlay)) {
    const isShort = overlay.strikeSides[rect.i] < 0;
    const color = isShort ? COLORS.strikeShort : COLORS.strike;
    ctx.fillStyle = "rgba(0,0,0,0.88)";
    ctx.fillRect(rect.x, rect.y - CHIP_H / 2, rect.w, CHIP_H);
    ctx.strokeStyle = color;
    ctx.lineWidth = draggingStrike === rect.i ? 1.8 : 1;
    ctx.strokeRect(rect.x, rect.y - CHIP_H / 2, rect.w, CHIP_H);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    if (!overlay.readOnly) {
      ctx.fillStyle = COLORS.axisText;
      ctx.fillText("−", rect.x + CHIP_ZONE / 2 + 1, rect.y);
      ctx.fillText("+", rect.x + rect.w - CHIP_ZONE / 2 - 1, rect.y);
    }
    ctx.fillStyle = color;
    ctx.fillText(rect.label, rect.x + rect.w / 2, rect.y);
  }
}

/** Vertical rail: the drag track for strike handles, just inside the price
 * axis so handles read directly against the scale. */
function drawRail(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  domain: [number, number],
  overlay: StrategyOverlay,
  draggingStrike: number | null,
) {
  if (overlay.readOnly) return; // position view: strikes aren't editable
  const railX = layout.plotW - RAIL_INSET;
  ctx.strokeStyle = "#2a2a2a";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(railX, 0);
  ctx.lineTo(railX, layout.volTop);
  ctx.stroke();
  overlay.strikes.forEach((strike, i) => {
    const y = priceToY(strike, domain, layout);
    if (y < 0 || y > layout.volTop) return;
    const isShort = overlay.strikeSides[i] < 0;
    const color = isShort ? COLORS.strikeShort : COLORS.strike;
    const r = draggingStrike === i ? 7 : 5;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(railX, y - r);
    ctx.lineTo(railX + r, y);
    ctx.lineTo(railX, y + r);
    ctx.lineTo(railX - r, y);
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = COLORS.bg;
    ctx.stroke();
  });
}

const IND_COLORS = {
  vwap: "#26C6DA",
  ema9: "#FFD54F",
  ema21: "#BA68C8",
  bb: "rgba(96,125,139,0.55)",
  bbFill: "rgba(96,125,139,0.08)",
  sma20: "#E0E0E0",
  sma50: "#7E57C2",
  sma200: "#EF6C00",
  rsi: "#B388FF",
  macd: "#2196F3",
  macdSignal: "#FFB000",
  oscGuide: "#333333",
};

/** Overlay indicator lines across the visible bar range. */
/** Theta-sell context: ±1σ expected-move cone from NOW to expiry. Short
 * strikes that sit inside the cone are the ones the market expects to
 * touch — the core visual for premium selling. */
function drawExpectedMove(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  view: ViewState,
  domain: [number, number],
  bars: Bars,
  overlay: StrategyOverlay,
  tfMinutes: number,
) {
  const nowIdx = bars.n - 1;
  const spot = overlay.spot;
  const sigma = overlay.sigma;
  const hours = overlay.hoursToExpiry;
  if (hours <= 0) return;
  const STEPS = 40;
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, layout.plotW, layout.plotH);
  ctx.clip();

  const upper: [number, number][] = [];
  const lower: [number, number][] = [];
  for (let i = 0; i <= STEPS; i++) {
    const h = (hours * i) / STEPS;
    const tau = h / TRADING_HOURS_PER_YEAR;
    const move = Math.exp(sigma * Math.sqrt(tau));
    const x = indexToX(futureIndex(h, nowIdx, tfMinutes), view, layout);
    upper.push([x, priceToY(spot * move, domain, layout)]);
    lower.push([x, priceToY(spot / move, domain, layout)]);
  }

  ctx.fillStyle = "rgba(255,166,0,0.05)";
  ctx.beginPath();
  upper.forEach(([x, y], i) => (i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)));
  for (let i = lower.length - 1; i >= 0; i--) ctx.lineTo(lower[i][0], lower[i][1]);
  ctx.closePath();
  ctx.fill();

  ctx.strokeStyle = "rgba(255,166,0,0.55)";
  ctx.lineWidth = 1;
  ctx.setLineDash([5, 4]);
  for (const line of [upper, lower]) {
    ctx.beginPath();
    line.forEach(([x, y], i) => (i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)));
    ctx.stroke();
  }
  ctx.setLineDash([]);
  const endUp = upper[upper.length - 1];
  ctx.fillStyle = "rgba(255,166,0,0.8)";
  ctx.textAlign = "right";
  ctx.fillText("±1σ EM", Math.min(endUp[0], layout.plotW) - 4, endUp[1] - 4);
  ctx.restore();
}

/** RSI / MACD panes stacked between the price pane and the volume strip,
 * each with its own scale, a header readout and axis marks. */
function drawOscillators(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  first: number,
  last: number,
  indicators: IndicatorToggles,
) {
  if (!bars.n || last <= first) return;
  let top = layout.oscTop;
  const panes: ("rsi" | "macd")[] = [];
  if (indicators.rsi) panes.push("rsi");
  if (indicators.macd) panes.push("macd");
  ctx.font = "10px 'SF Mono', Consolas, monospace";
  for (const pane of panes) {
    const h = layout.oscH;
    const bottom = top + h;
    ctx.strokeStyle = COLORS.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, top + 0.5);
    ctx.lineTo(layout.plotW, top + 0.5);
    ctx.stroke();
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, top, layout.plotW, h);
    ctx.clip();
    const pad = 4;
    const yOf = (v: number, lo: number, hi: number) => bottom - pad - ((v - lo) / (hi - lo || 1)) * (h - 2 * pad);
    const polyline = (values: Float64Array, lo: number, hi: number, color: string, width = 1.2) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.beginPath();
      let started = false;
      for (let i = first; i <= last; i++) {
        const x = indexToX(i, view, layout);
        const y = yOf(values[i], lo, hi);
        if (!isFinite(y)) { started = false; continue; }
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      }
      ctx.stroke();
    };
    let header = "";
    let axisMarks: [number, string][] = [];
    if (pane === "rsi") {
      const rsi = computeRsi(bars.c, bars.n, 14);
      for (const lvl of [30, 70]) {
        ctx.strokeStyle = IND_COLORS.oscGuide;
        ctx.setLineDash([2, 4]);
        ctx.beginPath();
        ctx.moveTo(0, yOf(lvl, 0, 100));
        ctx.lineTo(layout.plotW, yOf(lvl, 0, 100));
        ctx.stroke();
        ctx.setLineDash([]);
      }
      polyline(rsi, 0, 100, IND_COLORS.rsi);
      const lastV = rsi[bars.n - 1];
      header = `RSI 14  ${lastV.toFixed(1)}`;
      axisMarks = [[yOf(70, 0, 100), "70"], [yOf(30, 0, 100), "30"]];
    } else {
      const { macd, signal, hist } = computeMacd(bars.c, bars.n);
      let amp = 0;
      for (let i = first; i <= last; i++) {
        amp = Math.max(amp, Math.abs(macd[i]), Math.abs(signal[i]), Math.abs(hist[i]));
      }
      amp = amp || 1;
      const zero = yOf(0, -amp, amp);
      const barW = layout.plotW / view.barsVisible;
      const bodyW = Math.max(1, Math.min(barW * 0.6, 10));
      for (let i = first; i <= last; i++) {
        const x = indexToX(i, view, layout);
        const y = yOf(hist[i], -amp, amp);
        ctx.fillStyle = hist[i] >= 0 ? COLORS.volUp : COLORS.volDown;
        ctx.fillRect(x - bodyW / 2, Math.min(y, zero), bodyW, Math.max(1, Math.abs(zero - y)));
      }
      ctx.strokeStyle = IND_COLORS.oscGuide;
      ctx.beginPath();
      ctx.moveTo(0, zero);
      ctx.lineTo(layout.plotW, zero);
      ctx.stroke();
      polyline(macd, -amp, amp, IND_COLORS.macd);
      polyline(signal, -amp, amp, IND_COLORS.macdSignal);
      const n1 = bars.n - 1;
      header = `MACD 12·26·9  ${macd[n1].toFixed(2)} / ${signal[n1].toFixed(2)}  h ${hist[n1].toFixed(2)}`;
      axisMarks = [[zero, "0"]];
    }
    ctx.restore();
    ctx.fillStyle = COLORS.axisText;
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText(header, 6, top + 3);
    for (const [y, label] of axisMarks) {
      ctx.textBaseline = "middle";
      ctx.fillText(label, layout.plotW + 6, y);
    }
    top = bottom;
  }
}

function drawIndicators(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  domain: [number, number],
  first: number,
  last: number,
  indicators: IndicatorToggles,
) {
  if (!bars.n || last <= first) return;
  const line = (values: Float64Array, color: string, width = 1.2, dash: number[] = []) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash);
    ctx.beginPath();
    let started = false;
    for (let i = first; i <= last; i++) {
      const x = indexToX(i, view, layout);
      const y = priceToY(values[i], domain, layout);
      if (x < -20 || x > layout.plotW + 20 || !isFinite(y)) {
        started = false;
        continue;
      }
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.setLineDash([]);
  };

  if (indicators.bb) {
    const bb = computeBollinger(bars);
    // Band fill between upper and lower.
    ctx.fillStyle = IND_COLORS.bbFill;
    ctx.beginPath();
    let started = false;
    for (let i = first; i <= last; i++) {
      const x = indexToX(i, view, layout);
      const y = priceToY(bb.upper[i], domain, layout);
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else ctx.lineTo(x, y);
    }
    for (let i = last; i >= first; i--) {
      ctx.lineTo(indexToX(i, view, layout), priceToY(bb.lower[i], domain, layout));
    }
    ctx.closePath();
    ctx.fill();
    line(bb.upper, IND_COLORS.bb, 1, [3, 3]);
    line(bb.lower, IND_COLORS.bb, 1, [3, 3]);
    line(bb.mid, IND_COLORS.bb, 1);
  }
  if (indicators.ema) {
    line(computeEma(bars.c, bars.n, 9), IND_COLORS.ema9, 1.2);
    line(computeEma(bars.c, bars.n, 21), IND_COLORS.ema21, 1.2);
  }
  if (indicators.sma) {
    line(computeSma(bars.c, bars.n, 20), IND_COLORS.sma20, 1);
    line(computeSma(bars.c, bars.n, 50), IND_COLORS.sma50, 1);
    line(computeSma(bars.c, bars.n, 200), IND_COLORS.sma200, 1.4);
  }
  if (indicators.vwap) {
    line(computeVwap(bars), IND_COLORS.vwap, 1.4, [6, 3]);
  }
}

/**
 * Always-visible execution boundaries: horizontal guides + price-axis badges
 * at the underlying level where TP / SL would fire at the earliest active
 * time. Directly driven by the TP%/SL% fields (and contour drags). Off-scale
 * levels clamp to the axis edge with a direction arrow instead of vanishing.
 * Breakevens (model-free, at the active premium basis) get white badges.
 */
function drawExitLevels(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  domain: [number, number],
  surface: HeatmapResult,
  overlay: StrategyOverlay,
) {
  const drawLevel = (level: number | null, color: string, tag: string) => {
    if (level === null) return;
    const y = priceToY(level, domain, layout);
    const onScreen = y >= 0 && y <= layout.volTop;
    if (onScreen) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([1, 4]);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(layout.plotW, y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    const by = Math.max(8, Math.min(layout.volTop - 8, y));
    const text = onScreen ? `${tag} ${fmtPrice(level)}` : `${tag} ${y < 0 ? "↑" : "↓"}`;
    ctx.fillStyle = color;
    ctx.fillRect(layout.plotW, by - 8, layout.axisW, 16);
    ctx.fillStyle = COLORS.bg;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(text, layout.plotW + 4, by);
  };
  // Breakevens first so TP/SL badges draw over them on overlap.
  for (const be of overlay.breakevens.slice(0, 3)) {
    const y = priceToY(be, domain, layout);
    if (y >= 0 && y <= layout.volTop) {
      ctx.strokeStyle = COLORS.breakeven;
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 5]);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(layout.plotW, y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    const by = Math.max(8, Math.min(layout.volTop - 8, y));
    ctx.fillStyle = COLORS.breakeven;
    ctx.fillRect(layout.plotW, by - 8, layout.axisW, 16);
    ctx.fillStyle = COLORS.bg;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(`BE ${fmtPrice(be)}`, layout.plotW + 4, by);
  }
  drawLevel(firstFinite(surface.tpLine), COLORS.tp, "TP");
  drawLevel(firstFinite(surface.slLine), COLORS.sl, "SL");
}

/** Share-plan lines: entry (amber), stop (red), target (green) with their
 * $ and % on the plot, and the exit day as an orange vertical in trading
 * time. The dragged line draws heavier. Clipped to the price pane. */
function drawEquityPlan(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  domain: [number, number],
  view: ViewState,
  bars: Bars,
  eq: EquityPlan,
  tfMinutes: number,
  dragTarget: DragTarget | null,
) {
  const entryPx = Math.abs(eq.exits.entry);
  const line = (price: number, color: string, label: string, heavy: boolean, dash: number[]) => {
    const y = priceToY(price, domain, layout);
    if (y < 0 || y > layout.volTop) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = heavy ? 2 : 1;
    ctx.setLineDash(dash);
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(layout.plotW, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = "11px 'SF Mono', Consolas, monospace";
    const w = ctx.measureText(label).width + 10;
    const ly = Math.max(9, Math.min(layout.volTop - 9, y - 10));
    ctx.fillStyle = "rgba(0,0,0,0.85)";
    ctx.fillRect(6, ly - 8, w, 16);
    ctx.fillStyle = color;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(label, 11, ly);
  };
  const pctOf = (px: number) => `${px >= entryPx ? "+" : "−"}${(Math.abs(px - entryPx) / entryPx * 100).toFixed(1)}%`;
  const dollars = (px: number) => {
    const v = (px - entryPx) * eq.shares * (eq.exits.entry < 0 ? -1 : 1);
    return `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(0)}`;
  };
  line(entryPx, COLORS.last, `ENTRY ${fmtPrice(entryPx)} · ${eq.shares} sh`, false, [6, 4]);
  line(
    Math.abs(eq.exits.sl), COLORS.sl,
    `STOP ${pctOf(Math.abs(eq.exits.sl))} · ${dollars(Math.abs(eq.exits.sl))}`,
    dragTarget?.kind === "eqsl", [2, 3],
  );
  if (eq.exits.tp != null) {
    const p = eq.pTarget != null ? ` · P ${(eq.pTarget * 100).toFixed(0)}%` : "";
    line(
      Math.abs(eq.exits.tp), COLORS.tp,
      `TARGET ${pctOf(Math.abs(eq.exits.tp))} · ${dollars(Math.abs(eq.exits.tp))}${p}`,
      dragTarget?.kind === "eqtp", [2, 3],
    );
  }
  // Exit day: trading-time offset from the last bar.
  const anchorIdx = Math.max(bars.n - 1, 0);
  const x = indexToX(anchorIdx + (eq.holdDays * RTH_MINUTES) / tfMinutes, view, layout);
  if (x >= 0 && x <= layout.plotW) {
    ctx.strokeStyle = COLORS.timeStop;
    ctx.lineWidth = dragTarget?.kind === "eqts" ? 2 : 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, layout.volTop);
    ctx.stroke();
    ctx.setLineDash([]);
    const label = `EXIT +${eq.holdDays}d${eq.timeStopAuto ? " auto" : ""}`;
    ctx.font = "11px 'SF Mono', Consolas, monospace";
    const w = ctx.measureText(label).width + 10;
    const lx = Math.min(x + 4, layout.plotW - w - 2);
    ctx.fillStyle = "rgba(0,0,0,0.85)";
    ctx.fillRect(lx, 4, w, 16);
    ctx.fillStyle = COLORS.timeStop;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(label, lx + 5, 12);
  } else if (x > layout.plotW) {
    ctx.fillStyle = COLORS.timeStop;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(`EXIT +${eq.holdDays}d →`, layout.plotW - 4, 12);
  }
}

/** Axis badges for the share plan; off-scale levels show an arrow. */
function drawEquityBadges(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  domain: [number, number],
  eq: EquityPlan,
) {
  const badge = (price: number, color: string, tag: string) => {
    const y = priceToY(price, domain, layout);
    const onScreen = y >= 0 && y <= layout.volTop;
    const by = Math.max(8, Math.min(layout.volTop - 8, y));
    ctx.fillStyle = color;
    ctx.fillRect(layout.plotW, by - 8, layout.axisW, 16);
    ctx.fillStyle = COLORS.bg;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.font = "11px 'SF Mono', Consolas, monospace";
    ctx.fillText(onScreen ? `${tag} ${fmtPrice(price)}` : `${tag} ${y < 0 ? "↑" : "↓"}`, layout.plotW + 4, by);
  };
  badge(Math.abs(eq.exits.sl), COLORS.sl, "SL");
  if (eq.exits.tp != null) badge(Math.abs(eq.exits.tp), COLORS.tp, "TP");
}

/** A SHARE POSITION on the chart: the entry (price line + the purple ENTRY
 * vertical at the fill time), the open P/L shaded from entry to the last
 * close over the holding window, the stop / target with their % and $ at
 * the position's size, and the exit day. Editable lines carry ⇕ / ⇔. */
function drawEquityPosition(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  domain: [number, number],
  view: ViewState,
  bars: Bars,
  pos: EquityPosition,
  tfMinutes: number,
  dragTarget: DragTarget | null,
) {
  const anchorIdx = anchorIndexForMs(bars, pos.anchorMs);
  const x0 = indexToX(anchorIdx, view, layout);
  const lastIdx = Math.max(bars.n - 1, 0);
  const xNow = indexToX(lastIdx, view, layout);
  const last = bars.c[lastIdx];
  if (last > 0 && xNow > x0) {
    const yE = priceToY(pos.entryPx, domain, layout);
    const yL = priceToY(last, domain, layout);
    const win = pos.side * (last - pos.entryPx) >= 0;
    ctx.fillStyle = win ? "rgba(0,200,120,0.10)" : "rgba(255,70,70,0.10)";
    const left = Math.max(x0, 0);
    ctx.fillRect(left, Math.min(yE, yL), Math.min(xNow, layout.plotW) - left, Math.abs(yL - yE));
  }
  ctx.font = "11px 'SF Mono', Consolas, monospace";
  const line = (price: number, color: string, label: string, heavy: boolean, dash: number[]) => {
    const y = priceToY(price, domain, layout);
    if (y < 0 || y > layout.volTop) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = heavy ? 2 : 1;
    ctx.setLineDash(dash);
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(layout.plotW, y);
    ctx.stroke();
    ctx.setLineDash([]);
    const w = ctx.measureText(label).width + 10;
    const ly = Math.max(9, Math.min(layout.volTop - 9, y - 10));
    ctx.fillStyle = "rgba(0,0,0,0.85)";
    ctx.fillRect(6, ly - 8, w, 16);
    ctx.fillStyle = color;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(label, 11, ly);
  };
  const pctOf = (px: number) =>
    `${pos.side * (px - pos.entryPx) >= 0 ? "+" : "−"}${((Math.abs(px - pos.entryPx) / pos.entryPx) * 100).toFixed(1)}%`;
  const dollars = (px: number) => {
    const v = pos.side * (px - pos.entryPx) * pos.shares;
    return `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(0)}`;
  };
  const drag = pos.editable ? " ⇕" : "";
  line(pos.entryPx, COLORS.last, `ENTRY ${fmtPrice(pos.entryPx)} · ${pos.side > 0 ? "" : "SHORT "}${pos.shares} sh`, false, [6, 4]);
  if (pos.sl != null) {
    line(pos.sl, COLORS.sl, `STOP ${fmtPrice(pos.sl)} · ${pctOf(pos.sl)} · ${dollars(pos.sl)}${drag}`, dragTarget?.kind === "eqsl", [2, 3]);
  }
  if (pos.tp != null) {
    line(pos.tp, COLORS.tp, `TARGET ${fmtPrice(pos.tp)} · ${pctOf(pos.tp)} · ${dollars(pos.tp)}${drag}`, dragTarget?.kind === "eqtp", [2, 3]);
  }
  const vline = (x: number, color: string, text: string, width: number) => {
    if (x < 0 || x > layout.plotW) {
      if (x > layout.plotW) {
        ctx.fillStyle = color;
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";
        ctx.fillText(`${text} →`, layout.plotW - 4, 12);
      }
      return;
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, layout.volTop);
    ctx.stroke();
    ctx.setLineDash([]);
    const w = ctx.measureText(text).width + 10;
    const lx = Math.min(x + 4, layout.plotW - w - 2);
    ctx.fillStyle = "rgba(0,0,0,0.85)";
    ctx.fillRect(lx, 4, w, 16);
    ctx.fillStyle = color;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(text, lx + 5, 12);
  };
  vline(x0, "#9c8cff", `ENTRY ${fmtDayET(pos.anchorMs)} ${fmtTimeET(pos.anchorMs)}`, 1.2);
  if (pos.timeStopHours != null && pos.timeStopMs != null) {
    const xTs = indexToX(futureIndex(pos.timeStopHours, anchorIdx, tfMinutes), view, layout);
    vline(xTs, COLORS.timeStop, `${pos.editable ? "⇔ " : ""}EXIT ${fmtDayET(pos.timeStopMs)}`, dragTarget?.kind === "eqts" ? 2.4 : 1.2);
  }
}

function drawEquityPositionBadges(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  domain: [number, number],
  pos: EquityPosition,
) {
  const badge = (price: number, color: string, tag: string) => {
    const y = priceToY(price, domain, layout);
    const onScreen = y >= 0 && y <= layout.volTop;
    const by = Math.max(8, Math.min(layout.volTop - 8, y));
    ctx.fillStyle = color;
    ctx.fillRect(layout.plotW, by - 8, layout.axisW, 16);
    ctx.fillStyle = COLORS.bg;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.font = "11px 'SF Mono', Consolas, monospace";
    ctx.fillText(onScreen ? `${tag} ${fmtPrice(price)}` : `${tag} ${y < 0 ? "↑" : "↓"}`, layout.plotW + 4, by);
  };
  badge(pos.entryPx, COLORS.last, "IN");
  if (pos.sl != null) badge(pos.sl, COLORS.sl, "SL");
  if (pos.tp != null) badge(pos.tp, COLORS.tp, "TP");
}

function niceStep(span: number, maxTicks: number): number {
  const raw = span / maxTicks;
  const mag = 10 ** Math.floor(Math.log10(raw));
  for (const mult of [1, 2, 2.5, 5, 10]) {
    if (raw <= mult * mag) return mult * mag;
  }
  return 10 * mag;
}

function drawPriceGrid(ctx: CanvasRenderingContext2D, layout: Layout, domain: [number, number]) {
  const [lo, hi] = domain;
  const step = niceStep(hi - lo, Math.max(3, Math.floor(layout.volTop / 60)));
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  for (let p = Math.ceil(lo / step) * step; p <= hi; p += step) {
    const y = priceToY(p, domain, layout);
    ctx.strokeStyle = COLORS.grid;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(layout.plotW, y);
    ctx.stroke();
    ctx.fillStyle = COLORS.axisText;
    ctx.fillText(fmtPrice(p), layout.plotW + 6, y);
  }
}

function drawTimeAxis(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  first: number,
  last: number,
  overlay: StrategyOverlay | null,
  tfMinutes: number,
  anchorIdx: number,
  showEth = false,
) {
  const targetPx = 90;
  const step = Math.max(1, Math.round((view.barsVisible * targetPx) / layout.plotW));
  // Zoomed out past ~half a session per label, an index-stepped grid lands
  // on the same clock time every day ("04:15 04:15 04:15…"): switch to one
  // label per session, at its first bar, showing the date.
  const barsPerDay = (showEth ? 16 * 60 : 6.5 * 60) / tfMinutes;
  const dayMode = step * 2 >= barsPerDay;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  let lastLabelX = -Infinity;
  for (let i = Math.max(first, 0); i <= last; i++) {
    const isNewDay = i > 0 && fmtDayET(bars.t[i]) !== fmtDayET(bars.t[i - 1]);
    if (dayMode ? !isNewDay : !(i % step === 0 || isNewDay)) continue;
    const x = indexToX(i, view, layout);
    if (x < 0 || x > layout.plotW) continue;
    ctx.strokeStyle = COLORS.grid;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, layout.plotH);
    ctx.stroke();
    if (x - lastLabelX < 56) continue; // never overprint two labels
    lastLabelX = x;
    ctx.fillStyle = isNewDay ? COLORS.last : COLORS.axisText;
    const label = isNewDay
      ? fmtDayET(bars.t[i])
      : tfMinutes < 1
        ? fmtTimeSecET(bars.t[i])
        : fmtTimeET(bars.t[i]);
    ctx.fillText(label, x, layout.plotH + 6);
  }
  // Future-region ticks: +1h, +2h, ... in trading hours.
  if (overlay?.legs) {
    ctx.fillStyle = COLORS.expiry;
    const hteCeil = Math.ceil(overlay.hoursToExpiry);
    const hourStep = overlay.hoursToExpiry > 14 ? 6.5 : 1;
    for (let h = hourStep; h < hteCeil; h += hourStep) {
      const x = indexToX(futureIndex(h, anchorIdx, tfMinutes), view, layout);
      if (x < 0 || x > layout.plotW) continue;
      const label = hourStep === 6.5 ? `+${Math.round(h / 6.5)}d` : `+${h}h`;
      ctx.fillText(label, x, layout.plotH + 6);
    }
  }
}

function drawVolume(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  first: number,
  last: number,
) {
  let maxV = 0;
  for (let i = first; i <= last; i++) if (bars.v[i] > maxV) maxV = bars.v[i];
  if (!maxV) return;
  const barW = layout.plotW / view.barsVisible;
  const bodyW = Math.max(1, Math.min(barW * 0.7, 14));
  const volH = layout.plotH - layout.volStart - 1;
  for (let i = first; i <= last; i++) {
    const x = indexToX(i, view, layout);
    if (x < -barW || x > layout.plotW + barW) continue;
    const h = (bars.v[i] / maxV) * volH;
    ctx.fillStyle = bars.c[i] >= bars.o[i] ? COLORS.volUp : COLORS.volDown;
    ctx.fillRect(x - bodyW / 2, layout.plotH - h, bodyW, h);
  }
}

const RTH_START_MIN = 9 * 60 + 30; // 09:30 ET
const RTH_END_MIN = 16 * 60; //       16:00 ET

/** Extended-hours shading: bars outside 09:30–16:00 ET get a subtle tint so
 * pre/after-market price action reads as a different regime at a glance. */
function drawSessionZones(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  first: number,
  last: number,
  etOff: number,
) {
  const halfBar = layout.plotW / view.barsVisible / 2;
  ctx.fillStyle = "rgba(104,138,196,0.16)";
  let runStart: number | null = null;
  const flush = (endIdx: number) => {
    if (runStart === null) return;
    const x0 = Math.max(indexToX(runStart, view, layout) - halfBar, 0);
    const x1 = Math.min(indexToX(endIdx, view, layout) + halfBar, layout.plotW);
    if (x1 > x0) ctx.fillRect(x0, 0, x1 - x0, layout.plotH);
    runStart = null;
  };
  for (let i = first; i <= last; i++) {
    const minuteOfDay = (((bars.t[i] / 60_000 + etOff) % 1440) + 1440) % 1440;
    const extended = minuteOfDay < RTH_START_MIN || minuteOfDay >= RTH_END_MIN;
    if (extended && runStart === null) runStart = i;
    if (!extended) flush(i - 1);
  }
  flush(last);
}

/** Session-day boundaries: a full-height line + date chip at the first bar of
 * each new ET trading day, across ALL visible history. Day detection is pure
 * arithmetic on the cached ET offset — no per-bar Intl calls. */
function drawSessionBoundaries(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  first: number,
  last: number,
  etOff: number,
) {
  const dayIndex = (i: number) => Math.floor((bars.t[i] / 60_000 + etOff) / 1440);
  ctx.textBaseline = "top";
  ctx.textAlign = "left";
  for (let i = Math.max(first, 1); i <= last; i++) {
    if (dayIndex(i) === dayIndex(i - 1)) continue;
    const x = indexToX(i, view, layout) - layout.plotW / view.barsVisible / 2;
    if (x < 0 || x > layout.plotW) continue;
    ctx.strokeStyle = "#3a3a3a";
    ctx.setLineDash([2, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, layout.plotH);
    ctx.stroke();
    ctx.setLineDash([]);
    const label = fmtDayET(bars.t[i]).toUpperCase();
    ctx.fillStyle = "rgba(0,0,0,0.8)";
    const w = ctx.measureText(label).width + 8;
    ctx.fillRect(x + 2, 2, w, 13);
    ctx.fillStyle = COLORS.axisText;
    ctx.fillText(label, x + 6, 4);
  }
}

function drawLastPrice(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  domain: [number, number],
  liveClose?: number,
) {
  const lastClose = liveClose ?? bars.c[bars.n - 1];
  const y = priceToY(lastClose, domain, layout);
  if (y < 0 || y > layout.volTop) return;
  ctx.strokeStyle = COLORS.last;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(0, y);
  ctx.lineTo(layout.plotW, y);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = COLORS.last;
  ctx.fillRect(layout.plotW, y - 8, layout.axisW, 16);
  ctx.fillStyle = COLORS.bg;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(fmtPrice(lastClose), layout.plotW + 6, y);
}

function drawCrosshair(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  domain: [number, number],
  mouse: { x: number; y: number },
) {
  ctx.strokeStyle = COLORS.crosshair;
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.moveTo(mouse.x, 0);
  ctx.lineTo(mouse.x, layout.plotH);
  ctx.moveTo(0, mouse.y);
  ctx.lineTo(layout.plotW, mouse.y);
  ctx.stroke();
  ctx.setLineDash([]);

  const price = yToPrice(mouse.y, domain, layout);
  ctx.fillStyle = "#222222";
  ctx.fillRect(layout.plotW, mouse.y - 8, layout.axisW, 16);
  ctx.fillStyle = "#ffffff";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(fmtPrice(price), layout.plotW + 6, mouse.y);

  const index = Math.round(xToIndex(mouse.x, view, layout));
  if (index >= 0 && index < bars.n) {
    const parts = [
      fmtDayET(bars.t[index]) + " " + fmtTimeET(bars.t[index]),
      "O " + fmtPrice(bars.o[index]),
      "H " + fmtPrice(bars.h[index]),
      "L " + fmtPrice(bars.l[index]),
      "C " + fmtPrice(bars.c[index]),
      "V " + Intl.NumberFormat("en-US").format(bars.v[index]),
    ];
    ctx.fillStyle = "rgba(0,0,0,0.85)";
    ctx.fillRect(6, 6, 430, 18);
    ctx.fillStyle = bars.c[index] >= bars.o[index] ? COLORS.up : COLORS.down;
    ctx.textAlign = "left";
    ctx.fillText(parts.join("   "), 12, 15);
  }
}
