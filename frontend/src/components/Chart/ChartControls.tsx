import { TIMEFRAMES, useTradingStore, type Timeframe } from "../../store/tradingStore";
import { SymbolSearch } from "../SymbolSearch";

export function ChartControls() {
  const tf = useTradingStore((s) => s.tf);
  const setTf = useTradingStore((s) => s.setTf);

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
      <span className="ml-auto text-[11px] text-bb-muted">
        WHEEL zoom · AXIS wheel/drag y-scale · DRAG pan · DBLCLICK reset
      </span>
    </div>
  );
}
