/**
 * Bar-table engine with the same surface the Perspective library used to
 * provide (the resurrection dropped @perspective-dev — ~34 MB of wasm — and
 * kept its tiny consumed API: an indexed, observable bars table per
 * (symbol, timeframe)).
 *
 * Consumers: barFeed.ts writes via replaceBars/upsertBar; CandlePane reads
 * via barsTable(...).view(...).to_columns() and redraws on on_update.
 */

export type BarRow = { t: number; o: number; h: number; l: number; c: number; v: number };

const BAR_COLUMNS = ["t", "o", "h", "l", "c", "v"] as const;

type ViewOpts = {
  columns?: readonly string[];
  /** Only t-asc is ever requested; anything else falls back to t-asc too. */
  sort?: readonly (readonly [string, string])[];
};

export type BarView = {
  on_update(cb: () => void): void;
  to_columns(): Promise<Record<string, unknown[]>>;
  delete(): Promise<void>;
};

export type BarTable = {
  view(opts?: ViewOpts): Promise<BarView>;
  update(rows: BarRow[]): Promise<void>;
  clear(): Promise<void>;
};

class TableImpl implements BarTable {
  /** Indexed on t (ms) — update() upserts, like Perspective's index:"t". */
  private rows = new Map<number, BarRow>();
  private listeners = new Set<() => void>();

  async view(opts?: ViewOpts): Promise<BarView> {
    const cols = (opts?.columns ?? BAR_COLUMNS).filter((c) =>
      (BAR_COLUMNS as readonly string[]).includes(c),
    );
    const listeners = this.listeners;
    let mine: (() => void)[] = [];
    return {
      on_update: (cb) => {
        mine.push(cb);
        listeners.add(cb);
      },
      to_columns: async () => {
        const sorted = [...this.rows.values()].sort((a, b) => a.t - b.t);
        const out: Record<string, unknown[]> = {};
        for (const c of cols) out[c] = sorted.map((r) => r[c as keyof BarRow]);
        return out;
      },
      delete: async () => {
        for (const cb of mine) listeners.delete(cb);
        mine = [];
      },
    };
  }

  async update(rows: BarRow[]): Promise<void> {
    for (const r of rows) this.rows.set(r.t, r);
    if (rows.length) this.notify();
  }

  async clear(): Promise<void> {
    this.rows.clear();
    this.notify();
  }

  private notify(): void {
    for (const cb of [...this.listeners]) cb();
  }
}

const barTables = new Map<string, BarTable>();

/** One indexed bars table per (symbol, timeframe); created on demand. */
export function barsTable(symbol: string, tf: string): Promise<BarTable> {
  const key = `${symbol}:${tf}`;
  let existing = barTables.get(key);
  if (!existing) {
    existing = new TableImpl();
    barTables.set(key, existing);
  }
  return Promise.resolve(existing);
}

export async function replaceBars(symbol: string, tf: string, rows: BarRow[]): Promise<void> {
  const table = await barsTable(symbol, tf);
  await table.clear();
  if (rows.length) await table.update(rows);
}

export async function upsertBar(symbol: string, tf: string, row: BarRow): Promise<void> {
  const table = await barsTable(symbol, tf);
  await table.update([row]);
}
