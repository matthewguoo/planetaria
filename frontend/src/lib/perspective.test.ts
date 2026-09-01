import { describe, expect, it } from "vitest";
import { barsTable, replaceBars, upsertBar, type BarRow } from "./perspective";

const row = (t: number, c = 1): BarRow => ({ t, o: c, h: c, l: c, c, v: 10 });

describe("perspective shim (bars table)", () => {
  it("replace + to_columns returns t-sorted columns", async () => {
    await replaceBars("TEST1", "1m", [row(3000), row(1000), row(2000)]);
    const table = await barsTable("TEST1", "1m");
    const view = await table.view({ columns: ["t", "c"], sort: [["t", "asc"]] });
    const cols = await view.to_columns();
    expect(cols.t).toEqual([1000, 2000, 3000]);
    expect(cols.c).toHaveLength(3);
    expect(Object.keys(cols)).toEqual(["t", "c"]);
    await view.delete();
  });

  it("upsert replaces the bar at the same t (indexed) and fires on_update", async () => {
    await replaceBars("TEST2", "1m", [row(1000, 1), row(2000, 2)]);
    const table = await barsTable("TEST2", "1m");
    const view = await table.view();
    let updates = 0;
    view.on_update(() => updates++);
    await upsertBar("TEST2", "1m", row(2000, 9)); // same t: upsert
    await upsertBar("TEST2", "1m", row(3000, 3)); // new t: append
    const cols = await view.to_columns();
    expect(cols.t).toEqual([1000, 2000, 3000]);
    expect(cols.c).toEqual([1, 9, 3]);
    expect(updates).toBe(2);
    await view.delete();
  });

  it("delete() detaches the listener; clear() empties and notifies survivors", async () => {
    await replaceBars("TEST3", "1m", [row(1000)]);
    const table = await barsTable("TEST3", "1m");
    const dead = await table.view();
    let deadUpdates = 0;
    dead.on_update(() => deadUpdates++);
    await dead.delete();
    const live = await table.view();
    let liveUpdates = 0;
    live.on_update(() => liveUpdates++);
    await table.clear();
    expect(deadUpdates).toBe(0);
    expect(liveUpdates).toBe(1);
    expect((await live.to_columns()).t).toEqual([]);
    await live.delete();
  });

  it("tables are per (symbol, tf)", async () => {
    await replaceBars("TEST4", "1m", [row(1000)]);
    await replaceBars("TEST4", "5m", [row(5000)]);
    const m1 = await (await (await barsTable("TEST4", "1m")).view()).to_columns();
    const m5 = await (await (await barsTable("TEST4", "5m")).view()).to_columns();
    expect(m1.t).toEqual([1000]);
    expect(m5.t).toEqual([5000]);
  });
});
