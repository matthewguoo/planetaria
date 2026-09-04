import { describe, expect, it } from "vitest";

import type { CapabilitiesSummary, RiskSettings } from "./api";
import { capabilitiesFrom, capabilitiesSummaryLine } from "./capabilities";

const risk = { options_level: 3, equity_long_only: false } as RiskSettings;

const summary = (over: Partial<CapabilitiesSummary> = {}): CapabilitiesSummary => ({
  probed_at: "2026-09-04T14:31:00+00:00",
  running: false,
  derived: { options_level: 3, equity_shorts: true, cash_account: false, fractional: true, extended_hours: true },
  sources: { options_level: "probe", equity_shorts: "probe" },
  manual_action: null,
  level_provenance: "verified by probe",
  ...over,
});

describe("capabilitiesFrom", () => {
  it("floors a live server at 2 only while unprobed", () => {
    expect(capabilitiesFrom(risk, true).optionsLevel).toBe(2);
    expect(capabilitiesFrom(risk, true, summary()).optionsLevel).toBe(3);
    expect(capabilitiesFrom(risk, false).optionsLevel).toBe(3);
  });

  it("trusts the server's effective level once probed", () => {
    const eff = { ...risk, options_level: 2 };
    const c = capabilitiesFrom(eff, true, summary({ derived: { options_level: 2, equity_shorts: false } }));
    expect(c.optionsLevel).toBe(2);
    expect(c.spreadsAllowed).toBe(false);
    expect(c.verified).toBe(true);
    expect(c.levelSource).toBe("probe");
  });

  it("a verified no-shorts result wins over the stored toggle", () => {
    const c = capabilitiesFrom(risk, false, summary({ derived: { equity_shorts: false } }));
    expect(c.shortsAllowed).toBe(false);
    expect(capabilitiesFrom(risk, false, summary()).shortsAllowed).toBe(true);
  });

  it("summary line", () => {
    expect(capabilitiesSummaryLine(summary())).toBe("L3 · shorts · margin · fractional · ext-hours");
    expect(capabilitiesSummaryLine(summary({ derived: { options_level: 2, equity_shorts: false, cash_account: true } }))).toBe("L2 · long-only · cash");
    expect(capabilitiesSummaryLine(null)).toBe("");
  });
});
