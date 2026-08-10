/**
 * The banner is the "your stops aren't firing" alarm — the one component
 * whose silence must be trustworthy in both directions. These tests pin the
 * two-strike debounce, both alarm causes, and the stand-down paths.
 */

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EnforcementBanner } from "./EnforcementBanner";
import { useAccountStore } from "../store/accountStore";
import { getSystemState, type Plan, type SystemState } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../lib/api")>();
  return {
    ...mod,
    getSystemState: vi.fn(),
    getAccount: vi.fn(),
    getPositions: vi.fn(),
  };
});

const mocked = vi.mocked(getSystemState);

const held = (id: string) => ({ id, status: "filled" }) as unknown as Plan;
const sys = (monitored: string[]) =>
  ({ enforcer: { monitored_plan_ids: monitored } }) as unknown as SystemState;

/** Let the in-flight evaluate() finish: one turn for the awaited fetch, one
 * for the setState that follows it. */
const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

const tick = async (ms: number) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  await flush();
};

describe("EnforcementBanner", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocked.mockReset();
    useAccountStore.setState({ positions: [], untracked: [] });
  });
  afterEach(() => {
    cleanup(); // no vitest globals, so RTL cannot register its own
    vi.useRealTimers();
  });

  it("stays quiet on a flat book even when the engine is unreachable", async () => {
    mocked.mockRejectedValue(new Error("down"));
    render(<EnforcementBanner />);
    await tick(25_000);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(mocked).not.toHaveBeenCalled();
  });

  it("debounces: one bad poll is a race, not an alarm", async () => {
    useAccountStore.setState({ positions: [held("p1")] });
    mocked.mockResolvedValueOnce(sys([])); // strike 1: monitor arming a beat behind
    mocked.mockResolvedValue(sys(["p1"])); // then the enforcer catches up
    render(<EnforcementBanner />);
    await tick(0);
    expect(screen.queryByRole("alert")).toBeNull();
    await tick(10_000);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("two consecutive unmonitored polls raise the alarm; a good poll clears it", async () => {
    useAccountStore.setState({ positions: [held("p1")] });
    mocked.mockResolvedValue(sys([]));
    render(<EnforcementBanner />);
    await tick(0);
    expect(screen.queryByRole("alert")).toBeNull();
    await tick(10_000);
    expect(screen.getByRole("alert")).toHaveTextContent("UNMONITORED");
    mocked.mockResolvedValue(sys(["p1"]));
    await tick(10_000);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("an unreachable engine raises the software-only warning after two strikes", async () => {
    useAccountStore.setState({ positions: [held("p1")] });
    mocked.mockRejectedValue(new Error("ECONNREFUSED"));
    render(<EnforcementBanner />);
    await tick(0);
    await tick(10_000);
    expect(screen.getByRole("alert")).toHaveTextContent("ENGINE UNREACHABLE");
  });

  it("a book going flat stands the alarm down immediately", async () => {
    useAccountStore.setState({ positions: [held("p1")] });
    mocked.mockResolvedValue(sys([]));
    render(<EnforcementBanner />);
    await tick(0);
    await tick(10_000);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    act(() => {
      useAccountStore.setState({ positions: [] });
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
