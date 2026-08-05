/**
 * Pure presentation logic for the strategy runtime page. The invariant that
 * matters most: an ENABLED instance whose task is dead must read as loss-red
 * — that is the "your bot silently died" indicator, and it must never be
 * mistakable for healthy green or idle gray.
 */

import { describe, expect, it } from "vitest";
import type { StrategyDecision } from "../lib/api";
import {
  decisionColor,
  decisionSummary,
  formatAge,
  runtimeSummary,
  stateColor,
} from "./strategyRunnerStore";

describe("stateColor", () => {
  it("enabled + running is profit-green", () => {
    expect(stateColor("enabled", "running")).toBe("text-bb-profit");
  });
  it("enabled + DEAD task is loss-red, never green", () => {
    expect(stateColor("enabled", "DEAD (KeyError)")).toBe("text-bb-loss");
  });
  it("enabled but not yet running is orange (transitional)", () => {
    expect(stateColor("enabled", "not started")).toBe("text-bb-orange");
  });
  it("paused is orange, disabled is muted", () => {
    expect(stateColor("paused", "not running")).toBe("text-bb-orange");
    expect(stateColor("disabled", "not running")).toBe("text-bb-muted");
  });
});

describe("runtimeSummary", () => {
  it("compacts task + queue + errors + age", () => {
    expect(
      runtimeSummary({ task: "running", queue_size: 3, errors: 2, last_event_age_s: 42 }),
    ).toBe("running · q3 · 2 err · 42s ago");
  });
  it("omits zero-valued parts", () => {
    expect(runtimeSummary({ task: "running", queue_size: 0, errors: 0, last_event_age_s: null })).toBe(
      "running",
    );
  });
  it("dash when not running", () => {
    expect(runtimeSummary({ task: "not running" })).toBe("-");
  });
});

describe("formatAge", () => {
  it("seconds under a minute", () => expect(formatAge(59)).toBe("59s"));
  it("minutes under an hour", () => expect(formatAge(150)).toBe("3m"));
  it("hours above", () => expect(formatAge(5400)).toBe("1.5h"));
});

describe("decisionColor", () => {
  it("maps the journal's action vocabulary", () => {
    expect(decisionColor("placed")).toBe("text-bb-profit");
    expect(decisionColor("intent")).toBe("text-bb-amber");
    expect(decisionColor("rejected")).toBe("text-bb-loss");
    expect(decisionColor("error")).toBe("text-bb-loss");
    expect(decisionColor("skip")).toBe("text-bb-orange");
    expect(decisionColor("note")).toBe("text-bb-muted");
  });
});

function decision(detail: Record<string, unknown> | null, plan_id: string | null = null): StrategyDecision {
  return {
    id: 1, ts: "2026-08-05T02:00:00+00:00", strategy_id: "s", signal_ids: null,
    action: "note", dedupe_key: null, plan_id, detail,
  };
}

describe("decisionSummary", () => {
  it("prefers the human strings the runner journals", () => {
    expect(decisionSummary(decision({ skip: "tape 919s stale" }))).toBe("tape 919s stale");
    expect(decisionSummary(decision({ why: "dedupe: hit" }))).toBe("dedupe: hit");
    expect(decisionSummary(decision({ error: "on_event timeout" }))).toBe("on_event timeout");
  });
  it("compacts llm verdicts", () => {
    expect(
      decisionSummary(decision({ verdict: { direction: "bearish", confidence: "high" } })),
    ).toBe("verdict: bearish (high)");
  });
  it("falls back to plan id, then detail keys, then dash", () => {
    expect(decisionSummary(decision(null, "abcdef1234"))).toBe("plan abcdef12");
    expect(decisionSummary(decision({ foo: 1, bar: 2 }))).toBe("foo, bar");
    expect(decisionSummary(decision({}))).toBe("-");
  });
});
