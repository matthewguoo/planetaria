/**
 * Adopt an untracked broker position into the enforcer: stop %, target %,
 * time stop days. Shares always send explicit values (the option-sized
 * server defaults would be a same-day forced sale); options may adopt with
 * a 0% stop — the premium is the stop, the time stop is the expiry cutoff.
 */

import { useState } from "react";
import { adoptPositions, apiError, type UntrackedPosition } from "../../lib/api";
import { tradingDateAhead } from "../../lib/equityMath";
import { etWallToUtcIso } from "../../lib/et";
import { occLabel } from "../../lib/positionDetail";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
import { Btn, Stepper } from "./MobileUi";
import { Sheet } from "./Sheet";

export function AdoptSheet({ pos, onClose, onChart }: { pos: UntrackedPosition; onClose: () => void; onChart?: (pos: UntrackedPosition) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const risk = useAccountStore((s) => s.account?.risk);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const { live } = useTradingMode();
  const stock = pos.asset_class === "stock";
  const [slPct, setSlPct] = useState(stock ? 10 : Math.round((risk?.default_sl_pct ?? 0.5) * 100));
  const [tpPct, setTpPct] = useState(0);
  const [days, setDays] = useState(30);

  const basis = pos.avg_entry_price;
  const mult = stock ? 1 : 100;
  const units = Math.floor(Math.abs(pos.qty));
  const intrinsic = !stock && slPct === 0;
  const stopPrice = basis * (1 - slPct / 100);
  const lossAtStop = intrinsic ? basis * mult * units : (basis - stopPrice) * mult * units;

  const adopt = async () => {
    setBusy(true);
    setError(null);
    try {
      await adoptPositions([pos.symbol], {
        sl_pct: slPct / 100,
        ...(stock ? (tpPct > 0 ? { tp_pct: tpPct / 100 } : { tp_pct: 10 }) : {}),
        ...(stock ? { time_stop_utc: etWallToUtcIso(tradingDateAhead(days), "15:55") } : {}),
      });
      await refreshPositions();
      onClose();
    } catch (err) {
      setError(apiError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet
      title={`ADOPT · ${occLabel(pos)}`}
      onClose={onClose}
      right={onChart && (
        <button className="h-9 border border-bb-border px-3 text-[11px] tracking-widest text-bb-muted active:text-bb-amber" onClick={() => onChart(pos)}>
          CHART
        </button>
      )}
    >
      <div className="flex flex-col gap-3 px-3 py-3">
        <div data-numeric className="flex gap-4 text-[12px] text-bb-muted">
          <span>×{pos.qty}</span>
          <span>{basis.toFixed(2)} → {pos.current_price != null ? pos.current_price.toFixed(2) : "—"}</span>
          {stock && Math.abs(pos.qty) % 1 !== 0 && <span>{units} whole</span>}
        </div>
        <Stepper label={stock ? "STOP %" : "STOP % (0 = premium)"} value={slPct} set={setSlPct} step={stock ? 1 : 5} unit="%" min={stock ? 1 : 0} max={95} />
        {stock && <Stepper label="TARGET % (0 = run)" value={tpPct} set={setTpPct} step={5} unit="%" min={0} max={1000} />}
        {stock && <Stepper label="DAYS" value={days} set={setDays} step={5} unit="d" min={5} max={365} />}
        <div data-numeric className="text-[12px] text-bb-muted">
          {intrinsic ? (
            <>max loss <span className="text-bb-loss">-${lossAtStop.toFixed(0)}</span> · out at the expiry cutoff</>
          ) : (
            <>stop <span className="text-bb-loss">{stopPrice.toFixed(2)}</span> · <span className="text-bb-loss">-${lossAtStop.toFixed(0)}</span></>
          )}
        </div>
        <Btn kind={live ? "danger" : "primary"} disabled={busy} onClick={() => void adopt()}>
          {busy ? "…" : intrinsic ? `ADOPT · PREMIUM IS THE STOP${live ? " · LIVE" : ""}` : `ADOPT · ${slPct}% STOP${live ? " · LIVE" : ""}`}
        </Btn>
        {error && <div className="text-[11px] text-bb-loss">✗ {error}</div>}
      </div>
    </Sheet>
  );
}
