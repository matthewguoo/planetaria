import { beforeEach, describe, expect, it } from "vitest";
import { draftDirty, draftFor, useExitDraftStore } from "./exitDraftStore";

const seedA = { sl: 1.0, tp: 3.0, timeStopUtc: null };

describe("exitDraftStore", () => {
  beforeEach(() => useExitDraftStore.getState().clear());

  it("seeds once per key and keeps an edit across re-seeds of the same values", () => {
    const s = useExitDraftStore.getState();
    s.seedFor("p1", seedA);
    expect(draftFor("p1")).toEqual(seedA);
    expect(draftFor("p2")).toBeNull();
    s.set({ sl: 1.5 });
    expect(draftDirty()).toBe(true);
    s.seedFor("p1", { ...seedA });
    expect(draftFor("p1")?.sl).toBe(1.5);
  });

  it("follows moved server exits only while untouched; a new key replaces all", () => {
    const s = useExitDraftStore.getState();
    s.seedFor("p1", seedA);
    s.seedFor("p1", { ...seedA, sl: 1.2 });
    expect(draftFor("p1")?.sl).toBe(1.2);
    s.set({ tp: 4 });
    s.seedFor("p1", { ...seedA, sl: 1.4 });
    expect(draftFor("p1")).toEqual({ sl: 1.2, tp: 4, timeStopUtc: null });
    s.reset();
    expect(draftFor("p1")?.sl).toBe(1.4);
    s.seedFor("p2", { sl: null, tp: null, timeStopUtc: "2026-09-30T19:55:00.000Z" });
    expect(draftFor("p1")).toBeNull();
    expect(draftFor("p2")?.timeStopUtc).toBe("2026-09-30T19:55:00.000Z");
    expect(draftDirty()).toBe(false);
  });
});
