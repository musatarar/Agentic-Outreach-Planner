/**
 * Display formatting for /done. Every formatter is timezone-explicit and only
 * renders server-decided instants: the browser's zone would put a row on a
 * different calendar day than the page it is on.
 */

/** Intl throws RangeError on an unknown zone; never let that blank the page. */
function withZone(
  iso: string,
  timeZone: string,
  options: Intl.DateTimeFormatOptions,
): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  try {
    return new Intl.DateTimeFormat('en-GB', { ...options, timeZone }).format(parsed);
  } catch {
    return new Intl.DateTimeFormat('en-GB', options).format(parsed);
  }
}

/** `09:14:03` — the mono timestamp on every row, in the server's triage zone. */
export function formatTimeOfDay(iso: string, timeZone: string): string {
  return withZone(iso, timeZone, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

/** `08:41` — the ends of the session range, where seconds would be noise. */
export function formatHourMinute(iso: string, timeZone: string): string {
  return withZone(iso, timeZone, { hour: '2-digit', minute: '2-digit', hour12: false });
}

/** `4 Aug, 09:00` — a future return time (snooze) or a past boundary. */
export function formatDateTime(iso: string, timeZone: string): string {
  return withZone(iso, timeZone, {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

/** `4 Aug` — a date with no meaningful time of day. */
export function formatDate(iso: string, timeZone: string): string {
  return withZone(iso, timeZone, { day: 'numeric', month: 'short' });
}

/** `2026-07-28` day-grouping key for an instant, evaluated in the server's zone. */
export function dayKey(iso: string, timeZone: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso.slice(0, 10);
  try {
    // en-CA renders ISO-ordered dates, which is exactly the key we want.
    return new Intl.DateTimeFormat('en-CA', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(parsed);
  } catch {
    return iso.slice(0, 10);
  }
}

/**
 * `Tuesday, 28 July` from a bare `YYYY-MM-DD`, anchored at noon UTC and
 * formatted in UTC so the calendar day can never roll backwards.
 */
export function formatDayLabel(isoDate: string): string {
  const parsed = new Date(`${isoDate}T12:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return isoDate;
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: 'UTC',
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  }).format(parsed);
}

function trimDecimal(value: number): string {
  const fixed = value.toFixed(1);
  return fixed.endsWith('.0') ? fixed.slice(0, -2) : fixed;
}

/** `$28.4M` — book size sums get large, and the exact dollar is never the point. */
export function formatUsdCompact(usd: number): string {
  const abs = Math.abs(usd);
  const sign = usd < 0 ? '-' : '';
  if (abs >= 1_000_000_000) return `${sign}$${trimDecimal(abs / 1_000_000_000)}B`;
  if (abs >= 1_000_000) return `${sign}$${trimDecimal(abs / 1_000_000)}M`;
  if (abs >= 1_000) return `${sign}$${trimDecimal(abs / 1_000)}K`;
  return `${sign}$${Math.round(abs)}`;
}

/** `19m 07s` / `1h 04m` — how long the session took, from `elapsed_seconds`. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`;
  if (minutes > 0) return `${minutes}m ${String(secs).padStart(2, '0')}s`;
  return `${secs}s`;
}

/** `4:58` — the undo countdown. Always m:ss so the width never jumps. */
export function formatCountdown(msRemaining: number): string {
  const total = Math.max(0, Math.ceil(msRemaining / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}
