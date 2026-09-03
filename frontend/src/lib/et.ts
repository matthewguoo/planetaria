/**
 * ET wall-clock helpers. All of them read America/New_York time through one
 * Intl.DateTimeFormat formatToParts pass — the only DST-correct technique
 * without a tz library — so every ET-aware call site shares the same clock
 * instead of growing its own copy.
 */

const ET_FMT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  weekday: "short",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

export type EtParts = {
  /** "Mon" … "Sun" */
  weekday: string;
  year: number;
  month: number;
  day: number;
  /** 0-23 (Intl reports midnight as "24" in some engines; normalized here). */
  hour: number;
  minute: number;
  second: number;
};

export function etParts(ms: number = Date.now()): EtParts {
  const p = Object.fromEntries(ET_FMT.formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  return {
    weekday: p.weekday,
    year: Number(p.year),
    month: Number(p.month),
    day: Number(p.day),
    hour: Number(p.hour === "24" ? 0 : p.hour),
    minute: Number(p.minute),
    second: Number(p.second),
  };
}

/** Minutes since ET midnight. */
export function etMinutes(ms: number = Date.now()): number {
  const p = etParts(ms);
  return p.hour * 60 + p.minute;
}

/** ET calendar date as "YYYY-MM-DD". */
export function etDateIso(ms: number = Date.now()): string {
  const p = etParts(ms);
  return `${p.year}-${String(p.month).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
}

/** ET wall-clock offset (minutes to add to UTC epoch-minutes) at `ms`. */
export function etOffsetMinutes(ms: number = Date.now()): number {
  const p = etParts(ms);
  const wall = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return Math.round((wall - ms) / 60_000);
}

/** ET wall time (calendar date + "HH:MM") -> UTC ISO, using the ET offset
 * implied by the current instant — exact except across a DST turnover
 * between now and the target, where both order tickets accept the hour. */
export function etWallToUtcIso(dateIso: string, timeEt: string): string {
  const [y, mo, d] = dateIso.split("-").map(Number);
  const [h, mi] = timeEt.split(":").map(Number);
  return new Date(Date.UTC(y, mo - 1, d, h, mi) - etOffsetMinutes() * 60_000).toISOString();
}
