import { describe, expect, it } from "vitest";
import { cueForTransition } from "./audio";

const plan = (status: string, extra: Record<string, unknown> = {}) => ({
  id: "p1",
  status,
  ...extra,
});

describe("cueForTransition", () => {
  it("announces the entry lifecycle", () => {
    expect(cueForTransition("planned", plan("submitted"))).toBe("submitted");
    expect(cueForTransition("submitted", plan("partially_filled"))).toBe("partial");
    expect(cueForTransition("submitted", plan("filled"))).toBe("fill");
    expect(cueForTransition("partially_filled", plan("filled"))).toBe("fill");
  });

  it("announces exit triggers by reason", () => {
    expect(cueForTransition("filled", plan("exiting", { exit_reason: "tp" }))).toBe("tp");
    expect(cueForTransition("filled", plan("exiting", { exit_reason: "sl" }))).toBe("sl");
    expect(cueForTransition("filled", plan("exiting", { exit_reason: "time_stop" }))).toBe(
      "timeStop",
    );
    // Manual close: the user just clicked it, nothing to announce.
    expect(cueForTransition("filled", plan("exiting", { exit_reason: "manual" }))).toBeNull();
  });

  it("separates profitable and losing closes", () => {
    expect(cueForTransition("exiting", plan("closed", { realized_pnl: 120 }))).toBe(
      "closedProfit",
    );
    expect(cueForTransition("exiting", plan("closed", { realized_pnl: -80 }))).toBe("closedLoss");
    expect(cueForTransition("exiting", plan("closed", { realized_pnl: null }))).toBe(
      "closedProfit",
    );
  });

  it("announces rejections and cancels", () => {
    expect(cueForTransition("submitted", plan("rejected"))).toBe("rejected");
    expect(cueForTransition("submitted", plan("cancelled"))).toBe("cancelled");
  });

  it("is silent on field-only updates (same status)", () => {
    expect(cueForTransition("filled", plan("filled"))).toBeNull();
    expect(cueForTransition("exiting", plan("exiting", { exit_reason: "sl" }))).toBeNull();
  });

  it("never replays history for plans first seen already terminal", () => {
    expect(cueForTransition(undefined, plan("closed", { realized_pnl: 50 }))).toBeNull();
    expect(cueForTransition(undefined, plan("rejected"))).toBeNull();
    expect(cueForTransition(undefined, plan("cancelled"))).toBeNull();
  });

  it("does announce a first-seen live position (e.g. adopted from broker)", () => {
    expect(cueForTransition(undefined, plan("filled"))).toBe("fill");
  });
});
