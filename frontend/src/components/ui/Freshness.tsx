/**
 * Standard "data freshness" badge — one relative-time format + staleness color everywhere, so
 * every screen reports "how old is this data" the same way (fixes the mix of "Updated 20d 3h ago"
 * / "17d ago · cached" / per-row yellow pills / nothing).
 */
export function Freshness({
  ts,
  cached,
  prefix = "Updated",
  className = "",
}: {
  ts?: string | number | Date | null;
  cached?: boolean;
  prefix?: string;
  className?: string;
}) {
  if (ts == null) return null;
  const d = new Date(ts);
  const ms = d.getTime();
  if (Number.isNaN(ms)) return null;

  const ageMs = Date.now() - ms;
  const DAY = 86_400_000;
  const tone = ageMs > 30 * DAY ? "text-red-600" : ageMs > 7 * DAY ? "text-amber-600" : "text-gray-400";

  return (
    <span className={`text-[11px] ${tone} ${className}`} title={d.toLocaleString()}>
      {prefix} {relLabel(ageMs)}
      {cached ? " · cached" : ""}
    </span>
  );
}

function relLabel(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) {
    const rh = h % 24;
    return rh ? `${d}d ${rh}h ago` : `${d}d ago`;
  }
  const mo = Math.floor(d / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.floor(mo / 12)}y ago`;
}
