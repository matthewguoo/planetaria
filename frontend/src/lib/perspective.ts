/**
 * Perspective bootstrap — module scope, once per app (StrictMode-safe).
 *
 * Perspective is the single data engine: every bar that arrives goes into an
 * indexed Table; the candle pane, datagrids, and analytics all read from
 * Views of these tables.
 */

import perspective from "@perspective-dev/client";
import perspective_viewer from "@perspective-dev/viewer";
import "@perspective-dev/viewer-datagrid";
import "@perspective-dev/viewer-charts";

import SERVER_WASM from "@perspective-dev/server/dist/wasm/perspective-server.wasm?url";
import CLIENT_WASM from "@perspective-dev/viewer/dist/wasm/perspective-viewer.wasm?url";

await Promise.all([
  perspective.init_server(fetch(SERVER_WASM)),
  perspective_viewer.init_client(fetch(CLIENT_WASM)),
]);

export const psp = await perspective.worker();

const BAR_SCHEMA = {
  t: "datetime",
  o: "float",
  h: "float",
  l: "float",
  c: "float",
  v: "float",
} as const;

type Table = Awaited<ReturnType<typeof psp.table>>;

const barTables = new Map<string, Promise<Table>>();

/** One indexed bars table per (symbol, timeframe); created on demand. */
export function barsTable(symbol: string, tf: string): Promise<Table> {
  const key = `${symbol}:${tf}`;
  let existing = barTables.get(key);
  if (!existing) {
    existing = psp.table({ ...BAR_SCHEMA }, {
      index: "t",
      name: `bars_${symbol}_${tf}`,
    });
    barTables.set(key, existing);
  }
  return existing;
}

export type BarRow = { t: number; o: number; h: number; l: number; c: number; v: number };

export async function replaceBars(symbol: string, tf: string, rows: BarRow[]): Promise<void> {
  const table = await barsTable(symbol, tf);
  await table.clear();
  if (rows.length) await table.update(rows as unknown as Record<string, unknown>[]);
}

export async function upsertBar(symbol: string, tf: string, row: BarRow): Promise<void> {
  const table = await barsTable(symbol, tf);
  await table.update([row] as unknown as Record<string, unknown>[]);
}
