/**
 * Bind a component to the exit draft of one position: seeds the store for
 * the key on mount / when the plan's own exits move, returns the draft and
 * its setters. The chart reads the same store, so a price typed here moves
 * the line there and a line dragged there lands in the field here.
 */

import { useEffect } from "react";
import { sameExits, useExitDraftStore, type ExitDraft } from "../store/exitDraftStore";

export const planDraftKey = (planId: string) => `plan:${planId}`;
export const untrackedDraftKey = (symbol: string) => `untracked:${symbol}`;

export function useExitDraft(key: string, seed: ExitDraft) {
  const seedFor = useExitDraftStore((s) => s.seedFor);
  useEffect(() => {
    seedFor(key, seed);
    // seed is compared by value inside seedFor
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, seed.sl, seed.tp, seed.timeStopUtc, seedFor]);
  const draft = useExitDraftStore((s) => (s.key === key ? s.draft : null)) ?? seed;
  const storedSeed = useExitDraftStore((s) => (s.key === key ? s.seed : null)) ?? seed;
  const set = useExitDraftStore((s) => s.set);
  const reset = useExitDraftStore((s) => s.reset);
  return { draft, set, reset, dirty: !sameExits(draft, storedSeed) };
}
