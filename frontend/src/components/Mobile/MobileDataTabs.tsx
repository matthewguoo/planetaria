/**
 * Exchange-style data strip under the chart (Binance/TradingView mobile
 * pattern): the chart stays clean; the dense data lives in swappable tabs
 * in a fixed-height pane. Collapsible so the chart can take the whole
 * screen when you're just watching price.
 */

import { useMemo, useState } from "react";
import type { McResult } from "../../lib/mcSim";
import { useMonteCarlo, type McInputs } from "../../lib/useMonteCarlo";
import type { Designer } from "../../lib/useDesigner";
import { useStrategyStore } from "../../store/strategyStore";
import { useAccountStore } from "../../store/accountStore";
import { ChainPanel } from "../Chart/ChainPanel";
import { ThetaBlock } from "../Chart/ChartHud";
import { PositionsDrawer } from "../Positions/PositionsDrawer";

type DataTab = "stats" | "theta" | "chain" | "pos";

const pct = (v: number | null) => (v === null ? "—" : `${(v * 100).toFixed(0)}%`);
const usd = (v: number) => `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(0)}`;

function Cell({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="flex flex-col gap-0.5 px-2 py-1">
      <span className="text-[9px] tracking-wider text-bb-muted">{label}</span>
      <span data-numeric className={"text-[12px] " + (cls ?? "text-white")}>
        {value}
      </span>
    </div>
  );
}

/** MC + analytic probabilities as a stat grid (the desktop HUD's SIM block,
 * reshaped for a horizontal pane). Runs the worker only while visible. */
function StatsTab({ designer }: { designer: Designer }) {
  const volShift = useStrategyStore((s) => s.volShift);
  const skewBeta = useStrategyStore((s) => s.skewBeta);
  const [mc, setMc] = useState<McResult | null>(null);

  const mcInputs: McInputs | null = useMemo(() => {
    if (!designer.ready || !designer.legs) return null;
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
  }, [designer, volShift, skewBeta]);
  useMonteCarlo(mcInputs, setMc);

  const p = designer.probabilities;
  if (!designer.ready || !p) {
    return <div className="p-3 text-[11px] text-bb-muted">no strategy priced yet</div>;
  }
  return (
    <div className="grid grid-cols-4 gap-px">
      <Cell
        label="MC EV/SET"
        value={mc ? usd(mc.evPerSet) : "…"}
        cls={mc && mc.evPerSet >= 0 ? "text-bb-profit" : "text-bb-loss"}
      />
      <Cell label="WIN" value={mc ? pct(mc.winRate) : "…"} />
      <Cell
        label="EXITS TP·SL·T"
        value={mc ? `${Math.round(mc.pTp * 100)}·${Math.round(mc.pSl * 100)}·${Math.round((mc.pTime + mc.pExpiry) * 100)}` : "…"}
      />
      <Cell
        label="AVG TIME"
        value={
          mc
            ? mc.avgMinutesInTrade >= 90
              ? `${(mc.avgMinutesInTrade / 60).toFixed(1)}h`
              : `${Math.round(mc.avgMinutesInTrade)}m`
            : "…"
        }
      />
      <Cell label="P5·P50·P95" value={mc ? `${usd(mc.p5)}·${usd(mc.p50)}·${usd(mc.p95)}` : "…"} />
      <Cell label="P(PROFIT)" value={pct(p.pProfitExpiry)} />
      <Cell label="TOUCH TP/SL" value={`${pct(p.pTouchTp)}/${pct(p.pTouchSl)}`} />
      <Cell label="R:R" value={p.rr === null ? "—" : `${p.rr.toFixed(2)}:1`} />
    </div>
  );
}

export function MobileDataTabs({ designer }: { designer: Designer }) {
  const [tab, setTab] = useState<DataTab>("stats");
  const [open, setOpen] = useState(true);
  const positions = useAccountStore((s) => s.positions);

  const TABS: { id: DataTab; label: string }[] = [
    { id: "stats", label: "SIM" },
    { id: "theta", label: "THETA" },
    { id: "chain", label: "CHAIN" },
    { id: "pos", label: `POS${positions.length ? ` ${positions.length}` : ""}` },
  ];

  return (
    <div className="flex shrink-0 flex-col border-t border-bb-border bg-bb-panel">
      <div className="flex h-8 items-center">
        {TABS.map(({ id, label }) => (
          <button
            key={id}
            onClick={() => {
              setTab(id);
              setOpen(tab === id ? !open : true);
            }}
            className={
              "h-full flex-1 text-[11px] tracking-widest " +
              (open && tab === id
                ? "border-b-2 border-bb-amber font-semibold text-bb-amber"
                : "text-bb-muted")
            }
          >
            {label}
          </button>
        ))}
        <button
          className="h-full px-3 text-[11px] text-bb-muted"
          onClick={() => setOpen(!open)}
          aria-label={open ? "Collapse data pane" : "Expand data pane"}
        >
          {open ? "▾" : "▴"}
        </button>
      </div>
      {open && (
        <div className="h-[24dvh] min-h-36 overflow-y-auto border-t border-bb-border/60 bg-black">
          {tab === "stats" && <StatsTab designer={designer} />}
          {tab === "theta" && (
            <div className="p-1 text-[10px]">
              <ThetaBlock designer={designer} />
            </div>
          )}
          {tab === "chain" && <div className="h-full"><ChainPanel /></div>}
          {tab === "pos" && (
            <div className="flex h-full flex-col"><PositionsDrawer /></div>
          )}
        </div>
      )}
    </div>
  );
}
