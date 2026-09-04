/**
 * The fields a brokerage position page shows, for a managed plan or an
 * untracked broker position: quantity, basis, price, value, today,
 * protection, contract, expiry countdown, strike, underlying price, style,
 * size, open interest, last, bid/ask, breakeven, IV, greeks, volume.
 * Shared by the phone sheet and the desktop position panel. Missing data
 * renders "—", never a sentence.
 */

import type { HoldingDetail, Plan, UntrackedPosition } from "../../lib/api";
import { fmtUsd, pnlCls } from "../../lib/format";
import { planMaxLoss, planPremiumAtRisk, planStopRisk } from "../../lib/planRisk";
import {
  breakeven,
  breakevenPct,
  changeTodayUsd,
  countdown,
  expiryCountdown,
  heldQty,
  protection,
} from "../../lib/positionDetail";
import { fmtTimeET } from "../Chart/scales";
import { PROTECTION_LABEL } from "../Mobile/MobileUi";

export function Cell({ label, value, cls = "text-white", title, touch = false }: {
  label: string; value: React.ReactNode; cls?: string; title?: string; touch?: boolean;
}) {
  return (
    <div className={"flex flex-col gap-0.5 border-b border-bb-border/40 px-3 " + (touch ? "py-1.5" : "py-1")} title={title}>
      <span className={"tracking-widest text-bb-muted " + (touch ? "text-[10px]" : "text-[9px]")}>{label}</span>
      <span data-numeric className={(touch ? "text-[13px] " : "text-[12px] ") + cls}>{value}</span>
    </div>
  );
}

const n2 = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(2));
const n0 = (v: number | null | undefined) => (v == null ? "—" : Math.round(v).toLocaleString());

export function PositionDetails({ plan, pos, detail, equity, touch = false, cols }: {
  plan?: Plan | null;
  pos?: UntrackedPosition | null;
  detail: HoldingDetail | null;
  equity?: number | null;
  touch?: boolean;
  cols?: 2 | 3;
}) {
  const occ = plan ? (plan.legs.length === 1 && plan.legs[0].right ? {
    underlying: plan.underlying, expiry: plan.legs[0].expiry ?? "", right: plan.legs[0].right as "C" | "P", strike: plan.legs[0].strike ?? 0,
  } : null) : pos?.occ ?? null;
  const option = plan ? plan.asset_class !== "equity" : pos?.asset_class === "option";
  const single = plan ? plan.legs.length === 1 : true;
  const held = plan ? heldQty(plan) : Math.abs(pos?.qty ?? 0);
  const basis = plan ? (plan.fill_premium ?? plan.entry_limit) : (pos?.avg_entry_price ?? 0);
  const mult = option ? 100 : 1;
  const mark = plan ? plan.mark : pos?.current_price;
  const pnl = plan ? plan.unrealized_pnl : pos?.unrealized_pl;
  const costBasis = Math.abs(basis) * mult * held;
  const pnlPct = pnl != null && costBasis >= 1 ? (pnl / costBasis) * 100 : null;
  const prot = plan ? protection(plan) : "none";
  const symbol = plan ? plan.legs[0].symbol : pos?.symbol ?? "";
  const spot = detail?.underlying.spot ?? null;
  const dp = detail?.position;
  const q = detail?.quote ?? {};
  const c = detail?.contract ?? null;
  const lastday = dp?.lastday_price ?? pos?.lastday_price ?? null;
  const today = mark != null && lastday != null
    ? changeTodayUsd({ current_price: mark, lastday_price: lastday, qty: held, asset_class: option ? "option" : "stock" })
    : null;
  const be = option && single && occ ? breakeven(occ.right, occ.strike, basis) : null;
  const bePct = be != null ? breakevenPct(be, spot) : null;
  const exp = option && single && occ ? expiryCountdown(occ.expiry) : null;
  const grid = cols === 3 ? "grid-cols-3" : "grid-cols-2";
  const t = touch;

  return (
    <div className={"grid " + grid}>
      <Cell touch={t} label="QUANTITY" value={held} />
      <Cell touch={t} label="AVG ENTRY" value={n2(Math.abs(basis))} />
      <Cell touch={t} label="PRICE" value={mark != null ? n2(Math.abs(mark)) : "—"} title={plan?.mark_source === "broker" ? "broker position price" : undefined} />
      <Cell touch={t} label="MARKET VALUE" value={mark != null ? fmtUsd(Math.abs(mark) * mult * held) : "—"} />
      <Cell touch={t} label="COST BASIS" value={fmtUsd(costBasis)} />
      <Cell touch={t} label="UNREALIZED P/L" value={`${fmtUsd(pnl, true)}${pnlPct != null ? ` (${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(1)}%)` : ""}`} cls={pnlCls(pnl)} />
      <Cell touch={t} label="CHANGE TODAY" value={today != null ? fmtUsd(today, true) : "—"} cls={pnlCls(today)} />
      {plan ? (
        <>
          <Cell touch={t} label="STATUS" value={plan.status.toUpperCase()} cls={plan.status === "filled" ? "text-bb-profit" : "text-bb-orange"} />
          <Cell touch={t} label="PROTECTION" value={PROTECTION_LABEL[prot]} cls={prot === "stop" ? "text-bb-profit" : prot === "premium" ? "text-bb-amber" : "text-bb-loss"} />
          <Cell touch={t} label="STOP / TARGET" value={`${plan.sl_premium != null ? Math.abs(plan.sl_premium).toFixed(2) : "—"} / ${plan.tp_premium != null ? Math.abs(plan.tp_premium).toFixed(2) : "—"}`} />
          <Cell touch={t} label="TIME STOP" value={plan.time_stop_utc ? `${fmtTimeET(Date.parse(plan.time_stop_utc))} ET · ${countdown(plan.time_stop_utc)}` : "—"} cls="text-bb-orange" />
          <Cell
            touch={t}
            label="AT RISK"
            value={planPremiumAtRisk(plan) > 0 ? `-$${planPremiumAtRisk(plan).toFixed(0)} premium` : `-$${planStopRisk(plan).toFixed(0)} @ stop`}
            cls={planPremiumAtRisk(plan) > 0 ? "text-bb-amber" : "text-bb-orange"}
            title={equity ? `${((planMaxLoss(plan) / equity) * 100).toFixed(1)}% of equity` : undefined}
          />
        </>
      ) : (
        <Cell touch={t} label="PROTECTION" value="UNTRACKED · NO STOP" cls="text-bb-loss" />
      )}
      {option && single && occ && (
        <>
          <Cell touch={t} label="CONTRACT" value={symbol} />
          <Cell touch={t} label="EXPIRATION" value={`${occ.expiry || "—"}${exp ? ` · ${exp.label}` : ""}`} cls={exp && exp.dte === 0 ? "text-bb-orange" : "text-white"} />
          <Cell touch={t} label="UNDERLYING" value={occ.underlying} />
          <Cell touch={t} label="UNDERLYING PRICE" value={n2(spot)} />
          <Cell touch={t} label="STRIKE" value={occ.strike} />
          <Cell touch={t} label="TYPE" value={occ.right === "C" ? "call" : "put"} />
          <Cell touch={t} label="STYLE" value={c?.style ?? "—"} />
          <Cell touch={t} label="SIZE" value={c ? n0(c.size) : "—"} />
          <Cell touch={t} label="OPEN INTEREST" value={c ? n0(c.open_interest) : "—"} title={c?.open_interest_date ? `as of ${c.open_interest_date}` : undefined} />
          <Cell touch={t} label="VOLUME" value={n0(q.volume)} />
          <Cell touch={t} label="LAST" value={q.last != null ? `${n2(q.last)}${q.last_size ? ` ×${q.last_size}` : ""}` : "—"} />
          <Cell touch={t} label="BID / ASK" value={`${n2(q.bid)} / ${n2(q.ask)}`} />
          <Cell touch={t} label="BREAKEVEN" value={be != null ? n2(be) : "—"} />
          <Cell touch={t} label="BREAKEVEN %" value={bePct != null ? `${bePct >= 0 ? "+" : ""}${bePct.toFixed(2)}%` : "—"} cls={pnlCls(bePct)} />
          <Cell touch={t} label="IV" value={q.iv != null ? `${(q.iv * 100).toFixed(1)}%` : "—"} />
          <Cell touch={t} label="DELTA / THETA" value={`${q.delta != null ? q.delta.toFixed(2) : "—"} / ${q.theta != null ? q.theta.toFixed(2) : "—"}`} />
        </>
      )}
      {!option && (
        <>
          <Cell touch={t} label="LAST DAY CLOSE" value={n2(lastday)} />
          <Cell touch={t} label="BID / ASK" value={`${n2(q.bid)} / ${n2(q.ask)}`} />
        </>
      )}
      {plan?.exec_quality?.entry?.spread_capture != null && (
        <Cell touch={t} label="FILL QUALITY" value={`${Math.round((plan.exec_quality.entry.spread_capture as number) * 100)}% of spread kept`} />
      )}
    </div>
  );
}
