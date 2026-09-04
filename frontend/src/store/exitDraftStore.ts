/**
 * The exits being EDITED for the position in view — one draft shared by
 * the chart (drag the STOP / TARGET / EXIT lines) and the position panel
 * (type the prices). Seeded from the plan's rules (or an untracked
 * position's defaults) under a key; the chart draws the draft, the panel
 * submits it (ADOPT for an untracked position, APPLY for a plan's exits).
 *
 * Values use the plan convention: signed premium / share price on the
 * position-value axis (long = positive, short = negative), null = none.
 */

import { create } from "zustand";

export type ExitDraft = {
  sl: number | null;
  tp: number | null;
  /** UTC ISO; null = the plan's own default (options: the expiry cutoff). */
  timeStopUtc: string | null;
};

type State = {
  key: string | null;
  seed: ExitDraft;
  draft: ExitDraft;
  /** Seed for `key`. A new key replaces everything; the same key with new
   * seed values (the plan's exits moved server-side) follows them only
   * while the draft is untouched, so an in-progress edit survives a poll. */
  seedFor: (key: string, seed: ExitDraft) => void;
  set: (patch: Partial<ExitDraft>) => void;
  reset: () => void;
  clear: () => void;
};

const EMPTY: ExitDraft = { sl: null, tp: null, timeStopUtc: null };

export function sameExits(a: ExitDraft, b: ExitDraft): boolean {
  return a.sl === b.sl && a.tp === b.tp && a.timeStopUtc === b.timeStopUtc;
}

export const useExitDraftStore = create<State>((set, get) => ({
  key: null,
  seed: EMPTY,
  draft: EMPTY,
  seedFor: (key, seed) => {
    const s = get();
    if (s.key !== key) {
      set({ key, seed, draft: seed });
      return;
    }
    if (sameExits(s.seed, seed)) return;
    set({ seed, draft: sameExits(s.draft, s.seed) ? seed : s.draft });
  },
  set: (patch) => set((s) => ({ draft: { ...s.draft, ...patch } })),
  reset: () => set((s) => ({ draft: s.seed })),
  clear: () => set({ key: null, seed: EMPTY, draft: EMPTY }),
}));

/** The draft for `key`, or null when the store holds another position's. */
export function draftFor(key: string | null): ExitDraft | null {
  const s = useExitDraftStore.getState();
  return key !== null && s.key === key ? s.draft : null;
}

export function draftDirty(): boolean {
  const s = useExitDraftStore.getState();
  return !sameExits(s.draft, s.seed);
}
