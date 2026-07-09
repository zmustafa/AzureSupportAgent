// Shared formatting helpers used across views. Centralizes logic that was previously
// duplicated (error normalization, UTC-safe timestamp parsing, durations).

/** Normalize a thrown value into a clean message (strips a leading "Error:" prefix). */
export function formatError(e: unknown): string {
  return String(e).replace(/^Error:\s*/, "");
}

/**
 * Backend timestamps are UTC but SQLite returns them without a timezone designator
 * (e.g. "2026-06-06T08:40:00"), which JS would parse as LOCAL time. Append 'Z' when
 * no timezone is present so the value is correctly interpreted as UTC.
 */
export function ensureUtc(iso: string): string {
  if (!iso) return iso;
  return /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`;
}

/** Format an ISO timestamp as a localized time (today) or short date + time (older). */
export function formatTimestamp(iso?: string): string {
  if (!iso) return "";
  const d = new Date(ensureUtc(iso));
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (sameDay) return time;
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
}

/** Human-friendly duration: sub-second in ms, otherwise seconds (e.g. "4.2 sec"). */
export function formatDuration(ms?: number | null): string | undefined {
  if (ms == null) return undefined;
  if (ms < 1000) return `${ms} ms`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(1)} sec`;
  // A minute or more reads better as minutes than a large second count (e.g. 237 sec).
  let m = Math.floor(sec / 60);
  let s = Math.round(sec % 60);
  if (s === 60) {
    m += 1;
    s = 0;
  }
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

/** Compact relative time until/since an ISO timestamp, e.g. "in 3d 4h", "in 5m", "now", "2h ago". */
export function formatRelativeFromNow(iso?: string): string {
  if (!iso) return "";
  const d = new Date(ensureUtc(iso));
  if (Number.isNaN(d.getTime())) return "";
  let diff = Math.round((d.getTime() - Date.now()) / 1000); // seconds, signed
  const future = diff >= 0;
  diff = Math.abs(diff);
  if (diff < 45) return "now";
  const days = Math.floor(diff / 86400);
  const hours = Math.floor((diff % 86400) / 3600);
  const mins = Math.floor((diff % 3600) / 60);
  let body: string;
  if (days > 0) body = hours > 0 ? `${days}d ${hours}h` : `${days}d`;
  else if (hours > 0) body = mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
  else body = `${mins}m`;
  return future ? `in ${body}` : `${body} ago`;
}
