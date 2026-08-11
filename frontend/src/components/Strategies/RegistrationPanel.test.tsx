/**
 * The band-vs-live card: the one place the frozen pre-reg meets the live
 * record. Pins: in-band rendering, the unmeasured-metric honesty path
 * (nosip's drift metric), the UNREGISTERED state — and the discipline rule
 * that no live Sharpe ever appears at ladder sample sizes.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { RegistrationPanel } from "./StrategiesPage";
import type { LadderState, RegisteredBlock } from "../../lib/api";

const block: RegisteredBlock = {
  doc: "docs/pre-registration-gap-fail-fade.md",
  registered_commit: "246f21e",
  source_note: "research/open-window/notes/failed_gap_split_20260810.md",
  window: "2022-01..2026-08",
  metric: "net_bp_per_trade",
  metric_label: "net bp/trade (long leg)",
  band: [8, 15],
  backtest: { value: 23.9, basis: "gross at the auction print" },
  costs_assumed: "10bp round trip",
  ladder: [
    { stage: "note-mode", sessions: 20 },
    { stage: "one-share live", sessions: 10 },
  ],
};

const ladder: LadderState = {
  stage: "note-mode",
  target_sessions: 20,
  sessions_observed: 4,
  trades_closed: 3,
  samples: 3,
  metric: "net_bp_per_trade",
  metric_computed: true,
  metric_label: "net bp/trade (long leg)",
  running_metric: 11,
  band: [8, 15],
  band_status: "in",
};

afterEach(cleanup);

describe("RegistrationPanel", () => {
  it("renders band, sessions-toward-gate, and the running metric", () => {
    render(<RegistrationPanel name="gff-1" registered={block} ladder={ladder} />);
    expect(screen.getByText(/PRE-REGISTRATION — GFF-1/)).toBeTruthy();
    expect(screen.getByText("4/20")).toBeTruthy();
    expect(screen.getByText(/\+11\.0 \(in\)/)).toBeTruthy();
    expect(screen.getByText(/8–15 net bp\/trade/)).toBeTruthy();
    // the discipline rule: no live Sharpe beside a frozen backtest number
    expect(screen.queryByText(/sharpe/i)).toBeNull();
  });

  it("says 'not yet measured' for a metric the engine cannot compute", () => {
    render(
      <RegistrationPanel
        name="nosip-1"
        registered={{ ...block, metric: "drift_hit_rate_nonneutral" }}
        ladder={{
          ...ladder,
          metric_computed: false,
          running_metric: null,
          band_status: null,
        }}
      />,
    );
    expect(screen.getByText(/not yet measured by the engine/)).toBeTruthy();
  });

  it("renders UNREGISTERED when the kind has no frozen block", () => {
    render(<RegistrationPanel name="pead" registered={null} ladder={null} />);
    expect(screen.getByText(/UNREGISTERED/)).toBeTruthy();
  });
});
