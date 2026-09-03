/**
 * The phone position cards move real money on the live server, so the
 * two-tap contract is pinned: nothing closes or adopts on a single tap,
 * the confirm strip names the position, KEEP backs out, and the untracked
 * card adopts with the stop the user set (shares get explicit sl/tp and a
 * dated time stop — never the option-sized server defaults).
 */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MobilePositions } from "./MobilePositions";
import { useAccountStore } from "../../store/accountStore";
import {
  adoptPositions,
  closePosition,
  getSystemState,
  type Plan,
  type UntrackedPosition,
} from "../../lib/api";

vi.mock("../../lib/api", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...mod,
    getSystemState: vi.fn(),
    closePosition: vi.fn(),
    adoptPositions: vi.fn(),
    getPositions: vi.fn(),
    getAccount: vi.fn(),
  };
});

const plan = {
  id: "abc12345",
  underlying: "AAPX",
  status: "filled",
  asset_class: "equity",
  legs: [{ symbol: "AAPX", right: null, strike: null, expiry: null, side: 1, ratio: 1, entry: 20, iv: null }],
  qty: 100,
  filled_qty: 100,
  entry_limit: 20,
  fill_premium: 20,
  mark: 21,
  unrealized_pnl: 100,
  tp_premium: 24,
  sl_premium: 18,
  time_stop_utc: "2026-10-01T19:55:00Z",
} as unknown as Plan;

const untracked: UntrackedPosition = {
  symbol: "PLTZ",
  qty: 100,
  side: 1,
  asset_class: "stock",
  avg_entry_price: 10,
  current_price: 9.5,
  market_value: 950,
  unrealized_pl: -50,
  occ: null,
};

describe("MobilePositions", () => {
  beforeEach(() => {
    vi.mocked(getSystemState).mockResolvedValue({
      enforcer: { monitored_plan_ids: ["abc12345"] },
    } as never);
    vi.mocked(closePosition).mockResolvedValue({});
    vi.mocked(adoptPositions).mockResolvedValue([]);
    useAccountStore.setState({
      positions: [plan],
      untracked: [untracked],
      account: { equity: 11_000, risk: { default_sl_pct: 0.5 } } as never,
      refreshPositions: async () => {},
    });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("closes only after CONFIRM, and KEEP backs out", async () => {
    render(<MobilePositions />);
    fireEvent.click(screen.getByText("AAPX"));
    fireEvent.click(screen.getByText("CLOSE"));
    expect(closePosition).not.toHaveBeenCalled();
    expect(screen.getByText(/close AAPX ×100 at market/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("KEEP"));
    expect(screen.queryByText("CONFIRM CLOSE")).toBeNull();
    fireEvent.click(screen.getByText("CLOSE"));
    await act(async () => {
      fireEvent.click(screen.getByText("CONFIRM CLOSE"));
    });
    expect(closePosition).toHaveBeenCalledWith("abc12345");
  });

  it("adopts a share position with an explicit stop, target and dated time stop", async () => {
    render(<MobilePositions />);
    fireEvent.click(screen.getByText("PLTZ"));
    // default 10% stop on shares; bump to 12%
    const plus = screen.getAllByText("+")[0];
    fireEvent.click(plus);
    fireEvent.click(plus);
    expect(screen.getByText(/ADOPT WITH 12% STOP/)).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByText(/ADOPT WITH 12% STOP/));
    });
    expect(adoptPositions).toHaveBeenCalledTimes(1);
    const [symbols, opts] = vi.mocked(adoptPositions).mock.calls[0];
    expect(symbols).toEqual(["PLTZ"]);
    expect(opts?.sl_pct).toBeCloseTo(0.12, 9);
    expect(opts?.tp_pct).toBe(10); // "run": far target, not the 100% option default
    expect(opts?.time_stop_utc).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });

  it("shows the enforcer state and the risk at stop", async () => {
    render(<MobilePositions />);
    fireEvent.click(screen.getByText("AAPX"));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText(/ENFORCER WATCHING/)).toBeInTheDocument();
    expect(screen.getByText(/-\$200/)).toBeInTheDocument(); // (20-18) × 100 sh
  });
});
