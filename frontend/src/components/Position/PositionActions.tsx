/**
 * What a hand can do to a managed position — close all or N at market or
 * a limit, add, give or move the exits, cancel the orders under it — as
 * forms shared by the phone sheet and the desktop position panel. Money
 * actions are two taps and the button says the sentence.
 */

import { useState } from "react";
import { apiError, cancelOpenOrder, closePosition, tightenExits, type OpenOrder, type Plan } from "../../lib/api";
import { closeSentence, heldQty } from "../../lib/positionDetail";
import { planExits } from "../../lib/positionView";
import { planDraftKey, useExitDraft } from "../../lib/useExitDraft";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
import { Btn, Seg, Stepper } from "../Mobile/MobileUi";
import { ExitFields } from "./ExitFields";

type Mode = null | "close" | "exits";

function useAct() {
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
  return { busy, error, act };
}

/** CLOSE (all / N, MKT / LMT) and ADD. */
export function PositionCloseAdd({ plan, onAdd, onClosed, touch = false }: {
  plan: Plan; onAdd: (plan: Plan) => void; onClosed?: () => void; touch?: boolean;
}) {
  const { live } = useTradingMode();
  const { busy, error, act } = useAct();
  const [mode, setMode] = useState<Mode>(null);
  const [confirm, setConfirm] = useState(false);
  const single = plan.legs.length === 1;
  const leg = plan.legs[0];
  const held = heldQty(plan);
  const basis = plan.fill_premium ?? plan.entry_limit;
  const side: 1 | -1 = leg.side > 0 ? 1 : -1;
  const [qty, setQty] = useState(held);
  const [orderType, setOrderType] = useState<"market" | "limit">("market");
  const [limit, setLimit] = useState(Number(Math.abs(plan.mark ?? basis).toFixed(2)));
  const step = Math.max(0.01, Math.abs(basis) * 0.01);
  const all = qty >= held;
  const addAllowed = !(live && (!single || leg.side < 0));
  const sentence = closeSentence(side, qty, orderType, orderType === "limit" ? limit : null, all);
  const closable = ["partially_filled", "filled"].includes(plan.status);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-1">
        <Btn kind={mode === "close" ? (live ? "danger" : "primary") : "outline-danger"} className="flex-1" disabled={!closable} touch={touch} onClick={() => { setMode(mode === "close" ? null : "close"); setConfirm(false); }}>
          CLOSE
        </Btn>
        <Btn className="flex-1" disabled={!addAllowed} touch={touch} onClick={() => onAdd(plan)}>ADD</Btn>
      </div>
      {mode === "close" && (
        <div className={"flex flex-col gap-2 border p-2 " + (live ? "border-bb-loss" : "border-bb-border")}>
          <Stepper touch={touch} label="QTY" value={qty} set={(v) => { setQty(v); setConfirm(false); }} step={1} min={1} max={held} format={(v) => (v >= held ? `ALL ${held}` : String(v))} />
          <div className="flex items-center justify-between">
            <span className={"text-bb-muted " + (touch ? "text-[12px]" : "text-[10px]")}>TYPE</span>
            <Seg value={orderType} onChange={(v) => { setOrderType(v); setConfirm(false); }} options={[{ id: "market", label: "MKT" }, { id: "limit", label: "LMT" }]} danger={live} touch={touch} />
          </div>
          {orderType === "limit" && (
            <Stepper touch={touch} label="LIMIT" value={limit} set={(v) => { setLimit(v); setConfirm(false); }} step={step} min={0.01} format={(v) => v.toFixed(2)} />
          )}
          {all && orderType === "limit" && plan.tp_premium != null && (
            <div className="text-[11px] text-bb-muted">rests as the target · stop stays armed</div>
          )}
          {!all && plan.partial_exit && <div className="text-[11px] text-bb-orange">a partial close is already working</div>}
          <div className="flex gap-1">
            {confirm ? (
              <>
                <Btn kind="danger" className="flex-[2]" disabled={busy} touch={touch} onClick={() => act(
                  () => closePosition(plan.id, all ? { order_type: orderType, ...(orderType === "limit" ? { limit_price: limit } : {}) }
                                                   : { qty, order_type: orderType, ...(orderType === "limit" ? { limit_price: limit } : {}) }),
                  () => { setMode(null); setConfirm(false); if (all) onClosed?.(); },
                )}>
                  {busy ? "…" : `CONFIRM ${sentence}${live ? " · LIVE" : ""}`}
                </Btn>
                <Btn className="flex-1" touch={touch} onClick={() => setConfirm(false)}>KEEP</Btn>
              </>
            ) : (
              <Btn kind={live ? "danger" : "primary"} className="flex-1" disabled={busy || (!all && !!plan.partial_exit)} touch={touch} onClick={() => setConfirm(true)}>
                {sentence}{live ? " · LIVE" : ""}
              </Btn>
            )}
          </div>
        </div>
      )}
      {error && <div className="text-[11px] text-bb-loss">✗ {error}</div>}
    </div>
  );
}

/** The automation under a position: its exits (give a stop, move stop /
 * target, SL ▲ ½) and the broker orders the engine holds for it. */
export function PositionAutomation({ plan, orders, monitored, touch = false }: {
  plan: Plan; orders: OpenOrder[]; monitored: boolean | null; touch?: boolean;
}) {
  const { busy, error, act } = useAct();
  const defaultSl = useAccountStore((s) => s.account?.risk?.default_sl_pct) ?? 0.5;
  const [open, setOpen] = useState(false);
  const [cancelId, setCancelId] = useState<string | null>(null);
  const option = plan.asset_class !== "equity";
  const held = heldQty(plan);
  const basis = plan.fill_premium ?? plan.entry_limit;
  const side: 1 | -1 = basis >= 0 ? 1 : -1;
  const mult = option ? 100 : 1;
  // The draft the chart draws and this editor types into; seeded from the
  // plan's own exits so an untouched draft IS the plan.
  const { draft, set, reset, dirty } = useExitDraft(planDraftKey(plan.id), planExits(plan, null));
  const proposedSl = Number((basis - Math.abs(basis) * (option ? defaultSl : 0.1)).toFixed(2));
  const sl = draft.sl;
  const tp = draft.tp;
  const exitsPatch = () => ({
    ...(sl != null && (plan.sl_premium == null || Math.abs(sl - plan.sl_premium) >= 0.005) ? { sl_premium: Number(sl.toFixed(2)) } : {}),
    ...(tp != null && Math.abs(tp) > 0 && (plan.tp_premium == null || Math.abs(tp - plan.tp_premium) >= 0.005) ? { tp_premium: Number(tp.toFixed(2)) } : {}),
    ...(draft.timeStopUtc && draft.timeStopUtc !== plan.time_stop_utc ? { time_stop_utc: draft.timeStopUtc } : {}),
  });
  const editable = ["partially_filled", "filled"].includes(plan.status);
  const editing = open || dirty;
  const txt = touch ? "text-[12px]" : "text-[10px]";

  return (
    <div className="flex flex-col gap-2">
      <div className={"flex flex-wrap items-center gap-x-3 gap-y-1 " + txt}>
        <span className="text-bb-muted">STOP <span data-numeric className="text-bb-loss">{plan.sl_premium != null ? Math.abs(plan.sl_premium).toFixed(2) : "—"}</span></span>
        <span className="text-bb-muted">TARGET <span data-numeric className="text-bb-profit">{plan.tp_premium != null ? Math.abs(plan.tp_premium).toFixed(2) : "—"}</span></span>
        {plan.tp_order_id && <span className="text-bb-muted">TP RESTING @ BROKER</span>}
        {plan.partial_exit && <span className="text-bb-orange">PARTIAL ×{plan.partial_exit.qty} WORKING</span>}
        {monitored === false && editable && <span className="text-bb-loss">NOT MONITORED</span>}
        {monitored === true && <span className="text-bb-profit">● ENFORCER</span>}
      </div>
      <div className="flex gap-1">
        <Btn
          className="flex-1"
          disabled={!editable}
          touch={touch}
          onClick={() => {
            if (editing) {
              reset();
              setOpen(false);
            } else {
              // A stopless plan opens with a proposed stop on the chart.
              if (plan.sl_premium == null) set({ sl: proposedSl });
              setOpen(true);
            }
          }}
        >
          {editing ? "CANCEL" : plan.sl_premium == null ? "ADD STOP" : "EDIT EXITS"}
        </Btn>
        {plan.sl_premium != null && (
          <Btn className="flex-1" disabled={!editable || busy} touch={touch} onClick={() => act(() => tightenExits(plan.id, { sl_premium: Number((((plan.sl_premium ?? 0) + (plan.mark ?? basis)) / 2).toFixed(2)) }))}>
            SL ▲ ½
          </Btn>
        )}
      </div>
      {editing && (
        <div className="flex flex-col gap-2 border border-bb-border p-2">
          <ExitFields
            kind={option ? "option" : "stock"}
            side={side}
            basis={Math.abs(basis)}
            mult={mult}
            units={held}
            draft={draft}
            set={set}
            touch={touch}
            stopRequired={plan.sl_premium != null || !option}
            targetRemovable={plan.tp_premium == null}
            timeStop={option ? "fixed" : "date"}
          />
          {plan.sl_premium == null && sl != null && <div className="text-[11px] text-bb-amber">adds a stop · only tightens from here</div>}
          <Btn kind="primary" disabled={busy || Object.keys(exitsPatch()).length === 0} touch={touch} onClick={() => act(() => tightenExits(plan.id, exitsPatch()), () => setOpen(false))}>
            {busy ? "…" : plan.sl_premium == null && sl != null ? `ADD STOP ${Math.abs(sl).toFixed(2)}` : "APPLY"}
          </Btn>
        </div>
      )}
      {orders.length > 0 && (
        <div className="border-t border-bb-border/40">
          <div className={"py-1 tracking-widest text-bb-muted " + (touch ? "text-[10px]" : "text-[9px]")}>ORDERS {orders.length}</div>
          {orders.map((o) => (
            <div key={o.id} className="flex items-center gap-2 border-b border-bb-border/40 py-1">
              <div className="min-w-0 flex-1">
                <div className={"text-white " + (touch ? "text-[13px]" : "text-[11px]")}>
                  {o.role ? `${o.role.toUpperCase()} · ` : ""}{o.side.toUpperCase()} {o.qty ?? "—"} @ {o.limit_price != null ? o.limit_price.toFixed(2) : "MKT"}
                </div>
                <div data-numeric className="text-[10px] text-bb-muted">{o.type.toUpperCase()} · {o.status.toUpperCase()}</div>
              </div>
              {cancelId === o.id ? (
                <span className="flex gap-1">
                  <Btn kind="danger" touch={touch} onClick={() => act(() => cancelOpenOrder(o.id), () => setCancelId(null))}>CONFIRM</Btn>
                  <Btn touch={touch} onClick={() => setCancelId(null)}>KEEP</Btn>
                </span>
              ) : (
                <Btn kind="outline-danger" touch={touch} onClick={() => setCancelId(o.id)}>CANCEL</Btn>
              )}
            </div>
          ))}
        </div>
      )}
      {error && <div className="text-[11px] text-bb-loss">✗ {error}</div>}
    </div>
  );
}
