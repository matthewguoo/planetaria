/**
 * Trading-time arithmetic in ET: the chart's future region is continuous
 * RTH (09:30–16:00, weekdays), so a level "N trading hours after entry"
 * needs an instant, and an instant needs its trading-hour offset. Both
 * directions here, both tested. Exchange holidays are not skipped (the
 * enforcer's clock parks a holiday exit to the next session anyway).
 */

import { etDateIso, etParts, etWallToUtcIso } from "./et";
import { tradingHoursToExpiry } from "./optionsMath";

const OPEN_MIN = 9 * 60 + 30;
const CLOSE_MIN = 16 * 60;

/** Trading hours from `fromMs` to `toMs` (0 when `toMs` is not later). */
export function tradingHoursBetween(fromMs: number, toMs: number): number {
  if (!(toMs > fromMs)) return 0;
  const day = etDateIso(toMs);
  return Math.max(tradingHoursToExpiry(day, fromMs) - tradingHoursToExpiry(day, toMs), 0);
}

function nextWeekday(dateIso: string): string {
  const d = new Date(`${dateIso}T00:00:00Z`);
  do d.setUTCDate(d.getUTCDate() + 1);
  while (d.getUTCDay() === 0 || d.getUTCDay() === 6);
  return d.toISOString().slice(0, 10);
}

function isWeekend(dateIso: string): boolean {
  const dow = new Date(`${dateIso}T00:00:00Z`).getUTCDay();
  return dow === 0 || dow === 6;
}

/** The instant `hours` trading hours after `fromMs`, as UTC ISO. A start
 * outside RTH snaps forward to the next open; the result lands inside a
 * session, snapped to `snapMin` minutes. */
export function addTradingHours(fromMs: number, hours: number, snapMin = 5): string {
  const p = etParts(fromMs);
  let date = etDateIso(fromMs);
  let minute = p.hour * 60 + p.minute;
  if (isWeekend(date) || minute >= CLOSE_MIN) {
    date = nextWeekday(date);
    minute = OPEN_MIN;
  } else if (minute < OPEN_MIN) {
    minute = OPEN_MIN;
  }
  let left = Math.max(0, hours) * 60;
  for (;;) {
    const room = CLOSE_MIN - minute;
    if (left <= room + 1e-6) {
      minute += left;
      break;
    }
    left -= room;
    date = nextWeekday(date);
    minute = OPEN_MIN;
  }
  minute = Math.min(Math.round(minute / snapMin) * snapMin, CLOSE_MIN);
  const hh = String(Math.floor(minute / 60)).padStart(2, "0");
  const mm = String(minute % 60).padStart(2, "0");
  return etWallToUtcIso(date, `${hh}:${mm}`);
}

/** The exit-day convention for share plans: 15:55 ET on the instant's ET
 * date (a weekend instant rolls to Monday). */
export function shareExitDayIso(ms: number): string {
  let date = etDateIso(ms);
  if (isWeekend(date)) date = nextWeekday(date);
  return etWallToUtcIso(date, "15:55");
}
