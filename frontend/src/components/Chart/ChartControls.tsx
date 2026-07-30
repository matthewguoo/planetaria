import { useAccountStore } from "../../store/accountStore";
import { TIMEFRAMES, useTradingStore, type Timeframe } from "../../store/tradingStore";
import { useUiStore } from "../../store/uiStore";
import { SymbolSearch } from "../SymbolSearch";

/** Shown while the chart is inspecting a position instead of the designer.
 * Live plans resolve from the positions store; CLOSED trades from the
 * history snapshot — both get the same chip with an always-available ✕
 * (the history view previously rendered no chip at all: no way back). */
function PositionBanner() {
  const viewingPlanId = useUiStore((s) => s.viewingPlanId);
  const viewedHistorical = useUiStore((s) => s.viewedHistorical);
  const pnlMode = useUiStore((s) => s.pnlMode);
  const setPnlMode = useUiStore((s) => s.setPnlMode);
  const closePositionView = useUiStore((s) => s.closePositionView);
  const positions = useAccountStore((s) => s.positions);
  const plan = viewingPlanId
    ? positions.find((p) => p.id === viewingPlanId) ??
      (viewedHistorical?.id === viewingPlanId ? viewedHistorical : null)
    : null;
  if (!plan) return null;
  const closed = ["closed", "cancelled", "rejected"].includes(plan.status);

  const legsLabel = plan.legs
    .map((l) => `${l.side > 0 ? "+" : "−"}${l.ratio || 1}${l.right}${l.strike}`)
    .join(" ");

  return (
    <span
      className={
        "flex min-w-0 items-center gap-2 border px-2 py-0.5 " +
        (closed ? "border-bb-border bg-bb-hover/40" : "border-bb-amber/60 bg-bb-amber/10")
      }
    >
      <span className={"text-[10px] tracking-widest " + (closed ? "text-bb-muted" : "text-bb-amber")}>
        {closed ? `CLOSED · ${(plan.exit_reason ?? plan.status).toUpperCase()}` : "POSITION"}
      </span>
      <span className="truncate text-[11px] text-white">
        {plan.underlying} {legsLabel} ×{plan.filled_qty || plan.qty}
      </span>
      <span data-numeric className="text-[10px] text-bb-muted">
        {closed
          ? `${(plan.fill_premium ?? plan.entry_limit).toFixed(2)} → ${
              plan.exit_premium != null ? plan.exit_premium.toFixed(2) : "—"
            }`
          : `fill ${(plan.fill_premium ?? plan.entry_limit).toFixed(2)}`}
      </span>
      {closed && plan.realized_pnl != null && (
        <span
          data-numeric
          className={"text-[10px] " + (plan.realized_pnl >= 0 ? "text-bb-profit" : "text-bb-loss")}
        >
          {plan.realized_pnl >= 0 ? "+" : "−"}${Math.abs(plan.realized_pnl).toFixed(0)}
        </span>
      )}
      {!closed && (
        <span className="flex gap-px">
          {(["entry", "live"] as const).map((mode) => (
            <button
              key={mode}
              onClick={() => setPnlMode(mode)}
              title={
                mode === "entry"
                  ? "P/L surface as projected AT ENTRY: leg IVs frozen from the fill, no smile, no scenario shocks"
                  : "P/L surface from the LATEST chain greeks: current leg IVs + live smile, scenario shocks apply"
              }
              className={
                "px-1.5 py-0.5 text-[10px] " +
                (pnlMode === mode
                  ? "bg-bb-amber font-semibold text-black"
                  : "text-bb-muted hover:text-bb-amber")
              }
            >
              {mode === "entry" ? "ENTRY PROJ" : "LIVE GREEKS"}
            </button>
          ))}
        </span>
      )}
      <button
        className="px-1 text-[11px] text-bb-muted hover:text-bb-loss"
        onClick={closePositionView}
        title="Back to the strategy designer (any trading action also exits this view)"
      >
        ✕
      </button>
    </span>
  );
}

export function ChartControls() {
  const tf = useTradingStore((s) => s.tf);
  const setTf = useTradingStore((s) => s.setTf);
  const showEth = useTradingStore((s) => s.showEth);
  const toggleShowEth = useTradingStore((s) => s.toggleShowEth);
  const chainOpen = useUiStore((s) => s.chainOpen);
  const toggleChain = useUiStore((s) => s.toggleChain);

  return (
    <div className="flex items-center gap-1 border-b border-bb-border bg-bb-panel px-2 py-1">
      <SymbolSearch />
      <div className="mx-2 h-4 w-px bg-bb-border" />
      {TIMEFRAMES.map((option) => (
        <button
          key={option}
          onClick={() => setTf(option as Timeframe)}
          className={
            "px-2 py-0.5 " +
            (tf === option
              ? "bg-bb-amber font-semibold text-black"
              : "text-bb-muted hover:bg-bb-hover hover:text-bb-amber")
          }
        >
          {option.toUpperCase()}
        </button>
      ))}
      <button
        onClick={toggleShowEth}
        title="Extended hours: show pre-market / after-hours / overnight bars (shaded). Off = regular session only, days contiguous."
        className={
          "px-2 py-0.5 text-[11px] " +
          (showEth
            ? "bg-bb-hover font-semibold text-bb-amber"
            : "text-bb-muted hover:bg-bb-hover hover:text-bb-amber")
        }
      >
        ETH
      </button>
      <div className="mx-2 h-4 w-px bg-bb-border" />
      <button
        onClick={toggleChain}
        title="Options chain — click B/S on a contract to add it as a leg"
        className={
          "px-2 py-0.5 text-[11px] tracking-widest " +
          (chainOpen
            ? "bg-bb-amber font-semibold text-black"
            : "text-bb-muted hover:text-bb-amber")
        }
      >
        CHAIN
      </button>
      <div className="mx-2 h-4 w-px bg-bb-border" />
      <PositionBanner />
      <span className="ml-auto hidden text-[11px] text-bb-muted 2xl:inline">
        WHEEL zoom · AXIS wheel/drag y-scale · DRAG pan · DBLCLICK reset
      </span>
    </div>
  );
}
