/**
 * Adopt sheet on the phone: the untracked position's details and the shared
 * adopt form; CHART opens the underlying in its own mode.
 */

import { useState } from "react";
import { getHoldingDetail, type HoldingDetail, type UntrackedPosition } from "../../lib/api";
import { occLabel } from "../../lib/positionDetail";
import { usePoll } from "../../lib/usePoll";
import { AdoptForm } from "../Position/AdoptForm";
import { PositionDetails } from "../Position/PositionDetails";
import { Sheet } from "./Sheet";

export function AdoptSheet({ pos, onClose, onChart }: { pos: UntrackedPosition; onClose: () => void; onChart?: (pos: UntrackedPosition) => void }) {
  const [detail, setDetail] = useState<HoldingDetail | null>(null);
  usePoll(async (alive) => {
    try {
      const d = await getHoldingDetail(pos.symbol);
      if (alive()) setDetail(d);
    } catch {
      /* details are decoration here */
    }
  }, 5_000, [pos.symbol]);

  return (
    <Sheet
      title={`ADOPT · ${occLabel(pos)}`}
      onClose={onClose}
      tall
      right={onChart && (
        <button className="h-9 border border-bb-border px-3 text-[11px] tracking-widest text-bb-muted active:text-bb-amber" onClick={() => onChart(pos)}>
          CHART
        </button>
      )}
    >
      <div className="px-3 py-3">
        <AdoptForm pos={pos} onDone={onClose} touch />
      </div>
      <PositionDetails pos={pos} detail={detail} touch cols={2} />
    </Sheet>
  );
}
