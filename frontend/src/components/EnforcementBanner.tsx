/**
 * Loud, unmissable warning when open positions exist but exits are NOT being
 * enforced: the engine is the bracket (TP partially broker-resting; SL and
 * time stop software-only), so an unreachable backend or an unmonitored plan
 * means nothing will fire — the 2026-07-29 NVDA trade sailed through TP and
 * its time stop exactly this way while the engine was down.
 *
 * Debounced by two consecutive polls so an entry-fill race (monitor arming a
 * beat behind the fill) doesn't flash a false alarm.
 */

import { useEffect, useRef, useState } from "react";
import { getSystemState } from "../lib/api";
import { useAccountStore } from "../store/accountStore";

const POLL_MS = 10_000;
// Plan states in which contracts are (or may be) held — these need a watcher.
const HELD = new Set(["partially_filled", "filled", "exiting"]);

export function EnforcementBanner() {
  const positions = useAccountStore((s) => s.positions);
  const [alarm, setAlarm] = useState<string | null>(null);
  const strikesRef = useRef(0); // consecutive bad polls (debounce)
  const heldIds = positions.filter((p) => HELD.has(p.status)).map((p) => p.id);
  const heldKey = heldIds.join(",");

  useEffect(() => {
    if (!heldIds.length) {
      strikesRef.current = 0;
      setAlarm(null);
      return;
    }
    let live = true;
    const evaluate = async () => {
      let bad: string | null = null;
      try {
        const sys = await getSystemState();
        const watched = new Set(sys.enforcer.monitored_plan_ids);
        const unmonitored = heldIds.filter((id) => !watched.has(id));
        if (unmonitored.length) {
          bad = `${unmonitored.length} OPEN POSITION${unmonitored.length > 1 ? "S" : ""} UNMONITORED — TP/SL/time stop will not fire`;
        }
      } catch {
        bad = "ENGINE UNREACHABLE — exits are NOT enforced (SL + time stop are software-only)";
      }
      if (!live) return;
      strikesRef.current = bad ? strikesRef.current + 1 : 0;
      setAlarm(strikesRef.current >= 2 ? bad : null);
    };
    void evaluate();
    const id = window.setInterval(() => void evaluate(), POLL_MS);
    return () => {
      live = false;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [heldKey]);

  if (!alarm) return null;
  return (
    <div
      className="flex shrink-0 items-center justify-center gap-2 border border-bb-loss bg-bb-loss/20 px-2 py-1 text-[11px] font-semibold text-bb-loss"
      role="alert"
    >
      <span className="animate-pulse">⚠</span>
      {alarm}
      <span className="animate-pulse">⚠</span>
    </div>
  );
}
