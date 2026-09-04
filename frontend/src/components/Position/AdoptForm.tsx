/**
 * Adopt an untracked broker position into the enforcer. The exits are
 * PRICES — typed here or dragged on the chart (one shared draft): a stop,
 * an optional target, an exit day for shares. Options may adopt with no
 * stop at all: the premium is the stop, the time stop is the expiry cutoff.
 * Shares always send an explicit stop and a dated time stop (the
 * option-sized server defaults would be a same-day forced sale).
 */

import { useState } from "react";
import { adoptPositions, apiError, type UntrackedPosition } from "../../lib/api";
import { tradingDateAhead } from "../../lib/equityMath";
import { etWallToUtcIso } from "../../lib/et";
import { adoptSeed } from "../../lib/positionView";
import { untrackedDraftKey, useExitDraft } from "../../lib/useExitDraft";
import { useAccountStore, useTradingMode } from "../../store/accountStore";
import { Btn } from "../Mobile/MobileUi";
import { ExitFields } from "./ExitFields";

export function AdoptForm({ pos, onDone, touch = false }: { pos: UntrackedPosition; onDone?: () => void; touch?: boolean }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const defaultSl = useAccountStore((s) => s.account?.risk?.default_sl_pct) ?? 0.5;
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const { live } = useTradingMode();
  const stock = pos.asset_class === "stock";
  const side: 1 | -1 = pos.qty >= 0 ? 1 : -1;
  const basis = Math.abs(pos.avg_entry_price);
  const units = Math.floor(Math.abs(pos.qty));
  const { draft, set } = useExitDraft(untrackedDraftKey(pos.symbol), adoptSeed(pos, defaultSl));

  const slAbs = draft.sl == null ? null : Math.abs(draft.sl);
  const tpAbs = draft.tp == null ? null : Math.abs(draft.tp);
  const intrinsic = !stock && slAbs == null;
  // Percent distances the server sizes the plan from (side-aware).
  const slPct = slAbs == null ? 0 : Math.max((side * (basis - slAbs)) / basis, 0);
  const tpPct = tpAbs == null ? null : Math.max((side * (tpAbs - basis)) / basis, 0);
  const valid = basis > 0 && (intrinsic || (slAbs != null && slPct > 0 && slPct <= 0.95)) && (tpPct == null || tpPct > 0);

  const adopt = async () => {
    setBusy(true);
    setError(null);
    try {
      await adoptPositions([pos.symbol], {
        sl_pct: Number(slPct.toFixed(6)),
        ...(tpPct != null && tpPct > 0 ? { tp_pct: Number(Math.min(tpPct, 10).toFixed(6)) } : stock ? { tp_pct: 10 } : {}),
        ...(stock
          ? { time_stop_utc: draft.timeStopUtc ?? etWallToUtcIso(tradingDateAhead(30), "15:55") }
          : draft.timeStopUtc ? { time_stop_utc: draft.timeStopUtc } : {}),
      });
      await refreshPositions();
      onDone?.();
    } catch (err) {
      setError(apiError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-2">
      {stock && Math.abs(pos.qty) % 1 !== 0 && (
        <div data-numeric className="text-[11px] text-bb-muted">{units} whole shares adopt · the fraction stays untracked</div>
      )}
      <ExitFields
        kind={stock ? "stock" : "option"}
        side={side}
        basis={basis}
        mult={stock ? 1 : 100}
        units={units}
        draft={draft}
        set={set}
        touch={touch}
        stopRequired={stock}
        timeStop={stock ? "date" : "fixed"}
      />
      <Btn kind={live ? "danger" : "primary"} disabled={busy || !valid} touch={touch} onClick={() => void adopt()}>
        {busy ? "…" : intrinsic ? `ADOPT · PREMIUM IS THE STOP${live ? " · LIVE" : ""}` : `ADOPT · STOP ${(slAbs ?? 0).toFixed(2)}${live ? " · LIVE" : ""}`}
      </Btn>
      {error && <div className="text-[11px] text-bb-loss">✗ {error}</div>}
    </div>
  );
}
