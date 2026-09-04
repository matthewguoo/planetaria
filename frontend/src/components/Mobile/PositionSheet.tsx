/**
 * Everything about one position, full-screen: the fields a brokerage page
 * shows (quantity, basis, price, value, today, contract, expiry, strike,
 * underlying, style, size, open interest, last, breakeven, bid/ask, IV,
 * greeks, volume) and every action a hand can take — close all or some at
 * market or a limit, add, move the exits, cancel the orders under it. Money
 * actions are two taps, and the button says the sentence.
 */

import { useState } from "react";
import {
  apiError,
  cancelOpenOrder,
  closePosition,
  getHoldingDetail,
  tightenExits,
  type HoldingDetail,
  type OpenOrder,
  type Plan,
} from "../../lib/api";
import { fmtUsd, pnlCls } from "../../lib/format";
import { planMaxLoss, planPremiumAtRisk, planStopRisk } from "../../lib/planRisk";
import {
  breakeven,
  breakevenPct,
  changeTodayUsd,
  closeSentence,
  contractLabel,
  countdown,
  expiryCountdown,
  heldQty,
  protection,
} from "../../lib/positionDetail";
import { usePoll } from "../../lib/usePoll";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
import { fmtTimeET } from "../Chart/scales";
import { Btn, PROTECTION_LABEL, ProtectionDot, Seg, Stepper } from "./MobileUi";
import { Sheet } from "./Sheet";

type Mode = null | "close" | "exits";

function Cell({ label, value, cls = "text-white", title }: { label: string; value: React.ReactNode; cls?: string; title?: string }) {
  return (
    <div className="flex flex-col gap-0.5 border-b border-bb-border/40 px-3 py-1.5" title={title}>
      <span className="text-[10px] tracking-widest text-bb-muted">{label}</span>
      <span data-numeric className={"text-[13px] " + cls}>{value}</span>
    </div>
  );
}

const n2 = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(2));
const n0 = (v: number | null | undefined) => (v == null ? "—" : Math.round(v).toLocaleString());

export function PositionSheet({
  plan, orders, monitored, onClose, onChart, onAdd,
}: {
  plan: Plan;
  orders: OpenOrder[];
  monitored: boolean | null;
  onClose: () => void;
  onChart: (plan: Plan) => void;
  onAdd: (plan: Plan) => void;
}) {
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const equity = useAccountStore((s) => s.account?.equity);
  const { live } = useTradingMode();
  const [detail, setDetail] = useState<HoldingDetail | null>(null);
  const [mode, setMode] = useState<Mode>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [cancelId, setCancelId] = useState<string | null>(null);

  const single = plan.legs.length === 1;
  const leg = plan.legs[0];
  const option = plan.asset_class !== "equity";
  const held = heldQty(plan);
  const basis = plan.fill_premium ?? plan.entry_limit;
  const mult = option ? 100 : 1;
  const side: 1 | -1 = leg.side > 0 ? 1 : -1;
  const pnl = plan.unrealized_pnl;
  const costBasis = Math.abs(basis) * mult * held;
  const pnlPct = pnl != null && costBasis >= 1 ? (pnl / costBasis) * 100 : null;
  const prot = protection(plan);

  usePoll(async (alive) => {
    if (!single) return;
    try {
      const d = await getHoldingDetail(leg.symbol);
      if (alive()) setDetail(d);
    } catch {
      /* the sheet still shows what the plan knows */
    }
  }, 5_000, [leg.symbol]);

  // close form state
  const [qty, setQty] = useState(held);
  const [orderType, setOrderType] = useState<"market" | "limit">("market");
  const [limit, setLimit] = useState(Number(Math.abs(plan.mark ?? basis).toFixed(2)));
  const all = qty >= held;
  // exits form state. A plan with no stop yet (intrinsic cap, time-stop-only)
  // can be GIVEN one here; the steppers seed at a sane default and APPLY
  // sends whatever differs from the plan. Target 0 = none.
  const defaultSl = useAccountStore((s) => s.account?.risk?.default_sl_pct) ?? 0.5;
  const seedSl = plan.sl_premium ?? Number((basis - Math.abs(basis) * (option ? defaultSl : 0.1)).toFixed(2));
  const [sl, setSl] = useState(seedSl);
  const [tp, setTp] = useState(plan.tp_premium ?? 0);
  const step = Math.max(0.01, Math.abs(basis) * 0.01);
  const exitsPatch = () => ({
    ...(plan.sl_premium == null || sl !== plan.sl_premium ? { sl_premium: Number(sl.toFixed(2)) } : {}),
    ...(tp > 0 && tp !== plan.tp_premium ? { tp_premium: Number(tp.toFixed(2)) } : {}),
  });

  const act = async (fn: () => Promise<unknown>, done?: () => void) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await refreshPositions();
      done?.();
    } catch (err) {
      setError(apiError(err));
    } finally {
      setBusy(false);
    }
  };

  const spot = detail?.underlying.spot ?? null;
  const pos = detail?.position;
  const q = detail?.quote ?? {};
  const c = detail?.contract ?? null;
  const be = option && single && leg.right && leg.strike != null ? breakeven(leg.right, leg.strike, basis) : null;
  const bePct = be != null ? breakevenPct(be, spot) : null;
  const exp = option && single ? expiryCountdown(leg.expiry) : null;
  const today = pos ? changeTodayUsd({ current_price: pos.current_price, lastday_price: pos.lastday_price, qty: held, asset_class: option ? "option" : "stock" }) : null;
  const addAllowed = !(live && (!single || leg.side < 0));
  const sentence = closeSentence(side, qty, orderType, orderType === "limit" ? limit : null, all);

  return (
    <Sheet
      title={contractLabel(plan.legs, plan.underlying)}
      onClose={onClose}
      tall
      right={
        <button className="h-9 border border-bb-border px-3 text-[11px] tracking-widest text-bb-muted active:text-bb-amber" onClick={() => onChart(plan)}>
          CHART
        </button>
      }
    >
      {/* Head: the numbers that matter, then the dots. */}
      <div className="flex items-center gap-3 border-b border-bb-border px-3 py-2">
        <ProtectionDot state={prot} size={3} />
        <div data-numeric className="flex min-w-0 flex-1 items-baseline gap-3">
          <span className="text-[13px] text-bb-muted">×{held}</span>
          <span className={"text-[20px] font-semibold " + pnlCls(pnl)}>{fmtUsd(pnl, true)}</span>
          <span className={"text-[12px] " + pnlCls(pnl)}>{pnlPct != null ? `${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(1)}%` : ""}</span>
        </div>
        {monitored === false && <span className="text-[10px] tracking-widest text-bb-loss">NOT MONITORED</span>}
      </div>

      {/* Actions. */}
      <div className="flex gap-1 px-3 py-2">
        <Btn kind={mode === "close" ? (live ? "danger" : "primary") : "outline-danger"} className="flex-1" onClick={() => { setMode(mode === "close" ? null : "close"); setConfirm(false); }}>
          CLOSE
        </Btn>
        <Btn className="flex-1" disabled={!addAllowed} onClick={() => onAdd(plan)}>ADD</Btn>
        <Btn className="flex-1" disabled={!["partially_filled", "filled"].includes(plan.status)} onClick={() => setMode(mode === "exits" ? null : "exits")}>
          {plan.sl_premium == null ? "ADD STOP" : "EXITS"}
        </Btn>
      </div>

      {mode === "close" && (
        <div className={"mx-3 mb-2 flex flex-col gap-2 border p-2 " + (live ? "border-bb-loss" : "border-bb-border")}>
          <Stepper label="QTY" value={qty} set={(v) => { setQty(v); setConfirm(false); }} step={1} min={1} max={held} format={(v) => (v >= held ? `ALL ${held}` : String(v))} />
          <div className="flex items-center justify-between">
            <span className="text-[12px] text-bb-muted">TYPE</span>
            <Seg value={orderType} onChange={(v) => { setOrderType(v); setConfirm(false); }} options={[{ id: "market", label: "MKT" }, { id: "limit", label: "LMT" }]} danger={live} />
          </div>
          {orderType === "limit" && (
            <Stepper label="LIMIT" value={limit} set={(v) => { setLimit(v); setConfirm(false); }} step={step} min={0.01} format={(v) => v.toFixed(2)} />
          )}
          {all && orderType === "limit" && plan.tp_premium != null && (
            <div className="text-[11px] text-bb-muted">rests as the target · stop stays armed</div>
          )}
          {!all && plan.partial_exit && (
            <div className="text-[11px] text-bb-orange">a partial close is already working</div>
          )}
          <div className="flex gap-1">
            {confirm ? (
              <>
                <Btn kind="danger" className="flex-[2]" disabled={busy} onClick={() => act(
                  () => closePosition(plan.id, all ? { order_type: orderType, ...(orderType === "limit" ? { limit_price: limit } : {}) }
                                                   : { qty, order_type: orderType, ...(orderType === "limit" ? { limit_price: limit } : {}) }),
                  () => { setMode(null); setConfirm(false); if (all) onClose(); },
                )}>
                  {busy ? "…" : `CONFIRM ${sentence}${live ? " · LIVE" : ""}`}
                </Btn>
                <Btn className="flex-1" onClick={() => setConfirm(false)}>KEEP</Btn>
              </>
            ) : (
              <Btn kind={live ? "danger" : "primary"} className="flex-1" disabled={busy || (!all && !!plan.partial_exit)} onClick={() => setConfirm(true)}>
                {sentence}{live ? " · LIVE" : ""}
              </Btn>
            )}
          </div>
        </div>
      )}

      {mode === "exits" && (
        <div className="mx-3 mb-2 flex flex-col gap-2 border border-bb-border p-2">
          <Stepper label="STOP" value={sl} set={setSl} step={step} min={0} format={(v) => Math.abs(v).toFixed(2)} />
          <Stepper label="TARGET (0 = none)" value={tp} set={setTp} step={step} min={0} format={(v) => (v > 0 ? Math.abs(v).toFixed(2) : "—")} />
          <div data-numeric className="text-[11px] text-bb-muted">
            at stop <span className="text-bb-loss">-${(Math.max(basis - sl, 0) * mult * held).toFixed(0)}</span>
            {plan.sl_premium == null && <span className="ml-2 text-bb-amber">adds a stop · only tightens from here</span>}
          </div>
          <div className="flex gap-1">
            <Btn kind="primary" className="flex-[2]" disabled={busy || Object.keys(exitsPatch()).length === 0} onClick={() => act(() => tightenExits(plan.id, exitsPatch()), () => setMode(null))}>
              {busy ? "…" : plan.sl_premium == null ? `ADD STOP ${Math.abs(sl).toFixed(2)}` : "APPLY"}
            </Btn>
            {plan.sl_premium != null && (
              <Btn className="flex-1" onClick={() => act(() => tightenExits(plan.id, { sl_premium: Number((((plan.sl_premium ?? 0) + (plan.mark ?? basis)) / 2).toFixed(2)) }))}>
                SL ▲ ½
              </Btn>
            )}
          </div>
        </div>
      )}
      {error && <div className="px-3 pb-2 text-[11px] text-bb-loss">✗ {error}</div>}

      {/* Details. */}
      <div className="grid grid-cols-2">
        <Cell label="QUANTITY" value={held} />
        <Cell label="AVG ENTRY" value={n2(Math.abs(basis))} />
        <Cell label="PRICE" value={plan.mark != null ? n2(Math.abs(plan.mark)) : "—"} title={plan.mark_source === "broker" ? "broker position price" : undefined} />
        <Cell label="MARKET VALUE" value={plan.mark != null ? fmtUsd(Math.abs(plan.mark) * mult * held) : "—"} />
        <Cell label="COST BASIS" value={fmtUsd(costBasis)} />
        <Cell label="UNREALIZED P/L" value={`${fmtUsd(pnl, true)}${pnlPct != null ? ` (${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(1)}%)` : ""}`} cls={pnlCls(pnl)} />
        <Cell label="CHANGE TODAY" value={today != null ? fmtUsd(today, true) : "—"} cls={pnlCls(today)} />
        <Cell label="STATUS" value={plan.status.toUpperCase()} cls={plan.status === "filled" ? "text-bb-profit" : "text-bb-orange"} />
        <Cell label="PROTECTION" value={PROTECTION_LABEL[prot]} cls={prot === "stop" ? "text-bb-profit" : prot === "premium" ? "text-bb-amber" : "text-bb-loss"} />
        <Cell label="STOP / TARGET" value={`${plan.sl_premium != null ? Math.abs(plan.sl_premium).toFixed(2) : "—"} / ${plan.tp_premium != null ? Math.abs(plan.tp_premium).toFixed(2) : "—"}`} />
        <Cell label="TIME STOP" value={plan.time_stop_utc ? `${fmtTimeET(Date.parse(plan.time_stop_utc))} ET · ${countdown(plan.time_stop_utc)}` : "—"} cls="text-bb-orange" />
        <Cell label="AT RISK" value={planPremiumAtRisk(plan) > 0 ? `-$${planPremiumAtRisk(plan).toFixed(0)} premium` : `-$${planStopRisk(plan).toFixed(0)} @ stop`} cls={planPremiumAtRisk(plan) > 0 ? "text-bb-amber" : "text-bb-orange"} title={equity ? `${((planMaxLoss(plan) / equity) * 100).toFixed(1)}% of equity` : undefined} />
        {option && single && (
          <>
            <Cell label="CONTRACT" value={leg.symbol} />
            <Cell label="EXPIRATION" value={`${leg.expiry ?? "—"}${exp ? ` · ${exp.label}` : ""}`} cls={exp && exp.dte === 0 ? "text-bb-orange" : "text-white"} />
            <Cell label="UNDERLYING" value={plan.underlying} />
            <Cell label="UNDERLYING PRICE" value={n2(spot)} />
            <Cell label="STRIKE" value={leg.strike ?? "—"} />
            <Cell label="TYPE" value={leg.right === "C" ? "call" : "put"} />
            <Cell label="STYLE" value={c?.style ?? "—"} />
            <Cell label="SIZE" value={c ? n0(c.size) : "—"} />
            <Cell label="OPEN INTEREST" value={c ? n0(c.open_interest) : "—"} title={c?.open_interest_date ? `as of ${c.open_interest_date}` : undefined} />
            <Cell label="VOLUME" value={n0(q.volume)} />
            <Cell label="LAST" value={q.last != null ? `${n2(q.last)}${q.last_size ? ` ×${q.last_size}` : ""}` : "—"} />
            <Cell label="BID / ASK" value={`${n2(q.bid)} / ${n2(q.ask)}`} />
            <Cell label="BREAKEVEN" value={be != null ? n2(be) : "—"} />
            <Cell label="BREAKEVEN %" value={bePct != null ? `${bePct >= 0 ? "+" : ""}${bePct.toFixed(2)}%` : "—"} cls={pnlCls(bePct)} />
            <Cell label="IV" value={q.iv != null ? `${(q.iv * 100).toFixed(1)}%` : "—"} />
            <Cell label="DELTA / THETA" value={`${q.delta != null ? q.delta.toFixed(2) : "—"} / ${q.theta != null ? q.theta.toFixed(2) : "—"}`} />
          </>
        )}
        {!option && pos && (
          <>
            <Cell label="LAST DAY CLOSE" value={n2(pos.lastday_price)} />
            <Cell label="BID / ASK" value={`${n2(q.bid)} / ${n2(q.ask)}`} />
          </>
        )}
        {plan.exec_quality?.entry?.spread_capture != null && (
          <Cell label="FILL QUALITY" value={`${Math.round((plan.exec_quality.entry.spread_capture as number) * 100)}% of spread kept`} />
        )}
      </div>

      {/* Orders under this position. */}
      {orders.length > 0 && (
        <div className="mt-2 border-t border-bb-border">
          <div className="px-3 py-1.5 text-[10px] tracking-widest text-bb-muted">ORDERS {orders.length}</div>
          {orders.map((o) => (
            <div key={o.id} className="flex items-center gap-3 border-b border-bb-border/40 px-3 py-2">
              <div className="min-w-0 flex-1">
                <div className="text-[13px] text-white">
                  {o.role ? `${o.role.toUpperCase()} · ` : ""}{o.side.toUpperCase()} {o.qty ?? "—"} @ {o.limit_price != null ? o.limit_price.toFixed(2) : "MKT"}
                </div>
                <div data-numeric className="text-[11px] text-bb-muted">{o.type.toUpperCase()} · {o.status.toUpperCase()}</div>
              </div>
              {cancelId === o.id ? (
                <span className="flex gap-1">
                  <Btn kind="danger" onClick={() => act(() => cancelOpenOrder(o.id), () => setCancelId(null))}>CONFIRM</Btn>
                  <Btn onClick={() => setCancelId(null)}>KEEP</Btn>
                </span>
              ) : (
                <Btn kind="outline-danger" onClick={() => setCancelId(o.id)}>CANCEL</Btn>
              )}
            </div>
          ))}
        </div>
      )}
    </Sheet>
  );
}
