import { useNavigate } from "react-router-dom";

/**
 * The one affordance that takes you from "a principal appears in this row" to "tell me
 * everything about this principal".
 *
 * A single component on purpose: this appears in the IAM access grid, the Entra privileged
 * and application tables, the blast-radius graph and the Change Explorer actor cell. Six
 * hand-rolled links would drift in label, glyph and target, and the reader would have to
 * learn each one.
 *
 * It deliberately jumps ACROSS product surfaces (an /iam row lands on /entra/investigate).
 * Identity is one subject; splitting the destination by which screen you came from would
 * mean two half-answers.
 */
export function InvestigateLink({
  principalId,
  label,
  compact = true,
  title,
}: {
  /** Object id, UPN or appId — the resolver accepts any of them. */
  principalId: string;
  label?: string;
  compact?: boolean;
  title?: string;
}) {
  const navigate = useNavigate();
  if (!principalId) return null;
  const go = (e: React.MouseEvent) => {
    e.stopPropagation(); // rows are usually clickable themselves
    navigate(`/entra/investigate?principal_id=${encodeURIComponent(principalId)}`);
  };
  return (
    <button
      onClick={go}
      title={title ?? "Investigate this identity"}
      aria-label={`Investigate ${label ?? principalId}`}
      data-testid="investigate-link"
      className={
        compact
          ? "shrink-0 rounded px-1 text-gray-400 hover:bg-brand/10 hover:text-brand"
          : "inline-flex items-center gap-1 rounded border px-2 py-1 text-xs font-medium text-brand hover:bg-brand/10"
      }
    >
      🔍{compact ? "" : <span>{label ?? "Investigate"}</span>}
    </button>
  );
}
