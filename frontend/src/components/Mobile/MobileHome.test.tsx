/**
 * HOME moves real money on the live server, so its contracts are pinned:
 * rows render from the store, a row opens the position sheet, the close
 * form sends the exact qty/type/limit the user set and only after a second
 * tap, ALL closes at market by default, and adoption of shares carries an
 * explicit stop + dated time stop.
 */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MobileHome } from "./MobileHome";
import { useAccountStore } from "../../store/accountStore";
import {
  adoptPositions,
  closePosition,
  getAccountHistory,
  getHoldingDetail,
  getOpenOrders,
  tightenExits,
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
    getAccountHistory: vi.fn(),
    getOpenOrders: vi.fn(),
    getHoldingDetail: vi.fn(),
    flattenAll: vi.fn(),
    tightenExits: vi.fn(),
  };
});

const plan = {
  id: "abc12345",
  underlying: "NVDA",
  status: "filled",
  asset_class: "option",
  legs: [{ symbol: "NVDA260904P00230000", right: "P", strike: 230, expiry: "2026-09-04", side: 1, ratio: 1, entry: 2.07, iv: null }],
  qty: 5,
  filled_qty: 5,
  entry_limit: 2.07,
  fill_premium: 2.07,
  mark: 2.51,
  unrealized_pnl: 220,
  tp_premium: null,
  sl_premium: null,
  time_stop_utc: "2026-09-04T19:50:00Z",
  exit_fills: null,
  partial_exit: null,
} as unknown as Plan;

const untracked: UntrackedPosition = {
  symbol: "PLTZ", qty: 100, side: 1, asset_class: "stock", avg_entry_price: 9.03,
  current_price: 8.56, market_value: 856, unrealized_pl: -47, occ: null,
};

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

describe("MobileHome", () => {
  beforeEach(() => {
    vi.mocked(getSystemState).mockResolvedValue({ enforcer: { monitored_plan_ids: [plan.id] } } as never);
    vi.mocked(getAccountHistory).mockResolvedValue({ timestamps: [], equity: [], profit_loss: [], base_value: null });
    vi.mocked(getOpenOrders).mockResolvedValue([]);
    vi.mocked(getHoldingDetail).mockResolvedValue({
      position: { symbol: plan.legs[0].symbol, qty: 5, current_price: 2.51, lastday_price: 6.25 } as never,
      contract: { style: "american", size: 100, open_interest: 4635, open_interest_date: "2026-09-03", tradable: true, close_price: 2.51, underlying: "NVDA" },
      quote: { bid: 2.43, ask: 2.47, mid: 2.45, last: 6.25, last_size: 1, iv: 0.61, delta: -0.52, theta: -0.9, volume: 3300 },
      underlying: { symbol: "NVDA", spot: 229.7 },
    });
    vi.mocked(closePosition).mockResolvedValue({ ok: true });
    vi.mocked(adoptPositions).mockResolvedValue([]);
    useAccountStore.setState({
      positions: [plan],
      untracked: [untracked],
      account: { equity: 10_944, cash: 6058, day_realized_pnl: 0, mode: "paper", risk: { default_sl_pct: 0.5 } } as never,
      refreshPositions: async () => {},
    });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders the book as rows and opens the sheet with the contract details", async () => {
    render(<MobileHome onChart={() => {}} onChartUntracked={() => {}} onAdd={() => {}} onAccount={() => {}} />);
    await flush();
    expect(screen.getByText("POSITIONS 1")).toBeTruthy();
    expect(screen.getByText("NVDA 09/04 230P")).toBeTruthy();
    fireEvent.click(screen.getByTestId(`position-${plan.id}`));
    await flush();
    expect(screen.getByRole("dialog", { name: "NVDA 09/04 230P" })).toBeTruthy();
    expect(screen.getByText("OPEN INTEREST")).toBeTruthy();
    expect(screen.getByText("4,635")).toBeTruthy();
    expect(screen.getByText("BREAKEVEN")).toBeTruthy();
    expect(screen.getByText("227.93")).toBeTruthy();
    expect(screen.getByText("no stop · loss capped at the premium")).toBeTruthy();
  });

  it("partial close sends qty, type and limit only after CONFIRM", async () => {
    render(<MobileHome onChart={() => {}} onChartUntracked={() => {}} onAdd={() => {}} onAccount={() => {}} />);
    await flush();
    fireEvent.click(screen.getByTestId(`position-${plan.id}`));
    await flush();
    fireEvent.click(screen.getByText("CLOSE"));
    // qty 5 -> 2 (three taps down), LMT, limit up one step
    fireEvent.click(screen.getByLabelText("QTY down"));
    fireEvent.click(screen.getByLabelText("QTY down"));
    fireEvent.click(screen.getByLabelText("QTY down"));
    fireEvent.click(screen.getByText("LMT"));
    fireEvent.click(screen.getByLabelText("LIMIT up"));
    expect(closePosition).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("SELL 2 @ 2.53 LMT"));
    expect(closePosition).not.toHaveBeenCalled();          // first tap arms
    await act(async () => {
      fireEvent.click(screen.getByText(/CONFIRM SELL 2 @ 2.53 LMT/));
    });
    expect(closePosition).toHaveBeenCalledWith(plan.id, { qty: 2, order_type: "limit", limit_price: 2.53 });
  });

  it("ALL at market is the default close", async () => {
    render(<MobileHome onChart={() => {}} onChartUntracked={() => {}} onAdd={() => {}} onAccount={() => {}} />);
    await flush();
    fireEvent.click(screen.getByTestId(`position-${plan.id}`));
    await flush();
    fireEvent.click(screen.getByText("CLOSE"));
    fireEvent.click(screen.getByText("SELL ALL @ MKT"));
    await act(async () => {
      fireEvent.click(screen.getByText(/CONFIRM SELL ALL @ MKT/));
    });
    expect(closePosition).toHaveBeenCalledWith(plan.id, { order_type: "market" });
  });

  it("a plan with no stop can be given one from the sheet", async () => {
    vi.mocked(tightenExits).mockResolvedValue(plan);
    render(<MobileHome onChart={() => {}} onChartUntracked={() => {}} onAdd={() => {}} onAccount={() => {}} />);
    await flush();
    fireEvent.click(screen.getByTestId(`position-${plan.id}`));
    await flush();
    fireEvent.click(screen.getByText("ADD STOP"));
    // seeded at basis minus the account default (50%): 2.07 -> 1.03, one 2% step up
    fireEvent.click(screen.getByLabelText("STOP up"));
    await act(async () => {
      fireEvent.click(screen.getByText(/ADD STOP 1\.06/));
    });
    expect(tightenExits).toHaveBeenCalledWith(plan.id, { sl_premium: 1.05 });
  });

  it("adopting shares sends an explicit stop and a dated time stop", async () => {
    render(<MobileHome onChart={() => {}} onChartUntracked={() => {}} onAdd={() => {}} onAccount={() => {}} />);
    await flush();
    fireEvent.click(screen.getByTestId("untracked-PLTZ"));
    fireEvent.click(screen.getByLabelText("STOP % up"));
    fireEvent.click(screen.getByLabelText("STOP % up"));
    await act(async () => {
      fireEvent.click(screen.getByText(/ADOPT · 12% STOP/));
    });
    const [symbols, opts] = vi.mocked(adoptPositions).mock.calls[0];
    expect(symbols).toEqual(["PLTZ"]);
    expect(opts?.sl_pct).toBeCloseTo(0.12, 9);
    expect(opts?.tp_pct).toBe(10);
    expect(opts?.time_stop_utc).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });
});
