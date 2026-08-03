import { useNavigate } from "react-router-dom";

const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Escalation findings key on a graph NODE id ("principal::<guid>") rather than a bare one. */
const NODE_PREFIX = /^(principal|identity)::/;

/**
 * Kind strings that denote a PRINCIPAL, across three vocabularies that grew separately:
 * the Entra signal registry says `sp`, Investigate says `servicePrincipal`, the IAM signal
 * registry says `principal`.
 *
 * The exclusions are the load-bearing part:
 *   `app`     — an app-registration finding carries the APPLICATION object id, which is a
 *               different object from the service principal and resolves to nothing;
 *   `policy`  — a Conditional Access policy id is a GUID and would sail through the shape
 *               test below while meaning something else entirely;
 *   `tenant`, `scope`, `resource`, `role_definition`, `assignment`, `delegation` — not
 *               principals at all.
 */
const PRINCIPAL_KINDS = new Set([
  "user", "guest", "group", "sp", "serviceprincipal", "managedidentity", "mi", "principal",
]);

/**
 * The id to investigate, or null when this thing is not a principal we can resolve.
 *
 * Gated on the declared kind AND the shape of the id, because the kind alone lies: a signal
 * can declare a principal kind while carrying a credential id, an application object id or a
 * policy id in the same field. A deep link that resolves to nothing is worse than no link,
 * so anything that fails either test gets no affordance at all.
 *
 * One function rather than six, because six copies of this judgement is six chances for one
 * of them to be wrong in a way nobody notices — the link just quietly goes nowhere.
 */
export function investigatableId(
  kind: string | null | undefined, id: string | null | undefined,
): string | null {
  const k = String(kind ?? "").trim().toLowerCase();
  if (k && !PRINCIPAL_KINDS.has(k)) return null;
  const raw = String(id ?? "").trim().replace(NODE_PREFIX, "");
  if (!raw) return null;
  return GUID.test(raw) || raw.includes("@") ? raw : null;
}

/**
 * The one affordance that takes you from "a principal appears in this row" to "tell me
 * everything about this principal".
 *
 * A single component on purpose: this appears in the IAM access grid and findings, the Entra
 * privileged, activation, risk and findings tables, the group membership tree and the Change
 * Explorer actor cell. Ten hand-rolled links would drift in label, glyph and target, and the
 * reader would have to learn each one.
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
