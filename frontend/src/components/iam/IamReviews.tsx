/** Reviews tab — certification campaigns, decisions and the evidence pack.
 *
 * Three things on this screen exist to stop it producing a comfortable lie:
 *
 *  - a completed campaign shows its **completeness**, not just its status. "Completed" alone
 *    implies everything was reviewed; a campaign that closed with 40% of its items untouched is
 *    a different artifact and says so;
 *  - undecided items are never rendered as approved, and the copy says so explicitly;
 *  - a **self-attestation** campaign is labeled everywhere it appears. Self-review is not
 *    certification and must not be mistaken for it six months later.
 *
 * Remediation is displayed with its rollback and its `breaksIf` in the same block. The product
 * generates scripts; a human runs them.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type IamCampaign, type IamReviewItem } from "../../api";
import { useIamConnectionId } from "./IamShared";

const STATUS_CLASS: Record<string, string> = {
  draft: "bg-gray-100 text-gray-700",
  active: "bg-sky-100 text-sky-800",
  completed: "bg-emerald-100 text-emerald-800",
  expired: "bg-amber-100 text-amber-900",
  cancelled: "bg-gray-100 text-gray-500",
};

const SELECTORS: { label: string; value: Record<string, unknown> }[] = [
  { label: "All privileged access", value: { kind: "privileged" } },
  { label: "External access (guests, Lighthouse, multi-tenant SPs)", value: { kind: "external", include: ["guest", "lighthouse", "multi_tenant_sp"] } },
  { label: "Service principals", value: { kind: "principal_type", types: ["ServicePrincipal"] } },
  { label: "Everything the findings engine flagged", value: { kind: "signal", signal_ids: [] } },
];

function Completeness({ c }: { c: IamCampaign }) {
  const total = c.stats.total ?? 0;
  const decided = c.stats.decided ?? 0;
  if (c.status !== "completed" && c.status !== "expired") {
    return <span className="text-[11px] text-gray-600">{decided}/{total} decided</span>;
  }
  const complete = c.stats.complete ?? decided === total;
  return (
    <span
      data-testid="campaign-completeness"
      className={`text-[11px] ${complete ? "text-emerald-800" : "text-amber-900"}`}
    >
      {complete
        ? `complete — all ${total} items decided`
        : `INCOMPLETE — ${total - decided} of ${total} items were never decided (they were not approved)`}
    </span>
  );
}

function CampaignCard({ c, onOpen }: { c: IamCampaign; onOpen: (id: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => onOpen(c.id)}
      className="w-full rounded border bg-white p-3 text-left hover:bg-gray-50"
    >
      <div className="flex items-baseline gap-2">
        <span className={`rounded px-1.5 text-[10px] font-semibold uppercase ${STATUS_CLASS[c.status] ?? "bg-gray-100"}`}>
          {c.status}
        </span>
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-gray-800">{c.name}</span>
        {c.attestation_only && (
          <span
            data-testid="attestation-label"
            className="rounded bg-amber-100 px-1 text-[10px] text-amber-900"
            title="Principals reviewed their own access. Self-review is not independent certification."
          >
            self-attestation
          </span>
        )}
      </div>
      <div className="mt-1 flex items-baseline gap-3">
        <Completeness c={c} />
        {(c.stats.changed_since_baseline ?? 0) > 0 && (
          <span className="text-[11px] text-amber-800">
            {c.stats.changed_since_baseline} re-presented (access changed)
          </span>
        )}
        {(c.stats.unassigned ?? 0) > 0 && (
          <span className="text-[11px] text-amber-800">{c.stats.unassigned} unassigned</span>
        )}
      </div>
    </button>
  );
}

function ItemRow({
  item,
  campaignId,
  onDecided,
}: {
  item: IamReviewItem;
  campaignId: string;
  onDecided: () => void;
}) {
  const connectionId = useIamConnectionId();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const decide = useMutation({
    mutationFn: (decision: string) =>
      api.iamDecide(campaignId, item.id, { decision, reason }, connectionId),
    onSuccess: onDecided,
  });

  return (
    <div className={`rounded border bg-white ${item.changed_since_baseline ? "border-amber-400" : ""}`}>
      <button type="button" onClick={() => setOpen((v) => !v)} className="flex w-full items-baseline gap-2 px-3 py-2 text-left hover:bg-gray-50">
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-gray-800">{item.principalName}</span>
        <span className="shrink-0 text-[11px] text-gray-600">{item.roleName}</span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-gray-500" title={item.scope}>{item.scopeName || item.scope}</span>
        {item.decision ? (
          <span className="shrink-0 rounded bg-emerald-100 px-1 text-[10px] text-emerald-800">{item.decision}</span>
        ) : (
          <span className="shrink-0 rounded bg-gray-100 px-1 text-[10px] text-gray-600">undecided</span>
        )}
      </button>
      {open && (
        <div className="space-y-2 border-t px-3 py-2">
          {/* Re-presented, never silently updated. */}
          {item.changed_since_baseline && (
            <div className="rounded border border-amber-300 bg-amber-50 p-2 text-[11px] text-amber-900">
              This access changed since the campaign baseline. Any earlier decision was cleared —
              it was made about a different grant. Record a reason with your new decision.
            </div>
          )}
          <div className="text-[11px] text-gray-600">
            Held {item.context.why === "GroupTransitive" ? "through a group" : "directly"}
            {item.context.groupChain ? ` (${item.context.groupChain})` : ""}
            {item.context.standing ? " · standing privilege (nothing expires it)" : ""}
          </div>
          {(item.context.escalationPaths?.length ?? 0) > 0 && (
            <div className="text-[11px] text-red-800">
              Reaches full control via {item.context.escalationPaths!.length} escalation path(s).
            </div>
          )}
          {(item.context.openFindings?.length ?? 0) > 0 && (
            <ul className="space-y-0.5">
              {item.context.openFindings!.map((f) => (
                <li key={f.id} className="text-[11px] text-gray-700">{f.severity}: {f.title}</li>
              ))}
            </ul>
          )}
          {/* Never present unmeasured usage as unused. */}
          {item.context.usageNote && (
            <div data-testid="usage-note" className="text-[11px] text-amber-800">{item.context.usageNote}</div>
          )}

          <div className="flex flex-wrap items-center gap-1">
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Reason (required if this item was re-presented)"
              aria-label="Decision reason"
              className="min-w-0 flex-1 rounded border border-gray-300 px-1.5 py-1 text-xs"
            />
            {["approve", "revoke", "reduce", "needs_info"].map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => decide.mutate(d)}
                disabled={decide.isPending}
                className="rounded border bg-white px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
              >
                {d}
              </button>
            ))}
          </div>
          {decide.isError && (
            <div className="text-[11px] text-red-700">{(decide.error as Error).message}</div>
          )}
        </div>
      )}
    </div>
  );
}

function CampaignDetail({ id, onBack }: { id: string; onBack: () => void }) {
  const connectionId = useIamConnectionId();
  const qc = useQueryClient();
  const [format, setFormat] = useState("az");

  const q = useQuery({
    queryKey: ["iam", "campaign", id, connectionId],
    queryFn: () => api.iamCampaign(id, { connection_id: connectionId }),
  });
  const invalidate = () => qc.invalidateQueries({ queryKey: ["iam", "campaign", id] });

  const activate = useMutation({ mutationFn: () => api.iamActivateCampaign(id, connectionId), onSuccess: invalidate });
  const refresh = useMutation({ mutationFn: () => api.iamRefreshCampaign(id, connectionId), onSuccess: invalidate });
  const complete = useMutation({ mutationFn: () => api.iamCompleteCampaign(id, connectionId), onSuccess: invalidate });
  const evidence = useMutation({ mutationFn: () => api.iamCampaignEvidence(id, connectionId), onSuccess: invalidate });
  const remediate = useMutation({ mutationFn: () => api.iamCampaignRemediation(id, format, connectionId) });

  const c = q.data?.campaign;
  const items = q.data?.items ?? [];
  const bundle = remediate.data?.bundle;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b bg-white px-4 py-3">
        <div className="flex flex-wrap items-baseline gap-2">
          <button type="button" onClick={onBack} className="rounded border bg-white px-2 py-0.5 text-xs text-gray-700 hover:bg-gray-50">
            ← All reviews
          </button>
          <span className="text-sm font-semibold text-gray-800">{c?.name}</span>
          {c && <span className={`rounded px-1.5 text-[10px] font-semibold uppercase ${STATUS_CLASS[c.status] ?? ""}`}>{c.status}</span>}
          {c?.attestation_only && (
            <span data-testid="attestation-label" className="rounded bg-amber-100 px-1 text-[10px] text-amber-900">
              self-attestation — not independent certification
            </span>
          )}
          <div className="ml-auto flex flex-wrap items-center gap-1">
            {c?.status === "draft" && (
              <button type="button" onClick={() => activate.mutate()} className="rounded border bg-white px-2 py-1 text-xs hover:bg-gray-50">Activate</button>
            )}
            {c?.status === "active" && (
              <>
                <button type="button" onClick={() => refresh.mutate()} className="rounded border bg-white px-2 py-1 text-xs hover:bg-gray-50" title="Re-check every item against the current snapshot and re-present anything that moved.">
                  Re-check
                </button>
                <button type="button" onClick={() => complete.mutate()} className="rounded border bg-white px-2 py-1 text-xs hover:bg-gray-50">Complete</button>
              </>
            )}
            {(c?.status === "completed" || c?.status === "expired") && (
              <button type="button" onClick={() => evidence.mutate()} className="rounded border bg-white px-2 py-1 text-xs hover:bg-gray-50">
                Export evidence
              </button>
            )}
          </div>
        </div>
        {c && <div className="mt-1"><Completeness c={c} /></div>}
        {refresh.data && (
          <div className="mt-1 text-[11px] text-gray-600">
            {refresh.data.re_presented} item(s) re-presented · {refresh.data.confirmed} revocation(s) verified absent from the latest scan
            {refresh.data.reverted_claims > 0 && (
              <span className="text-red-700"> · {refresh.data.reverted_claims} marked applied but the access is still there</span>
            )}
          </div>
        )}
        {evidence.data && (
          <div className="mt-1 text-[11px] text-emerald-800">
            Evidence written. SHA-256 {evidence.data.evidence?.sha256 ?? evidence.data.digest}
          </div>
        )}
      </div>

      <div className="flex min-h-0 flex-1">
        <div className="min-h-0 flex-1 space-y-1.5 overflow-auto p-3">
          {q.isLoading && <div className="text-sm text-gray-500">Loading…</div>}
          {items.map((i) => (
            <ItemRow key={i.id} item={i} campaignId={id} onDecided={invalidate} />
          ))}
        </div>

        <div className="w-96 shrink-0 overflow-auto border-l bg-gray-50 p-3">
          <div className="mb-1 text-[11px] font-semibold uppercase text-gray-500">Remediation</div>
          <div className="flex items-center gap-1">
            <select value={format} onChange={(e) => setFormat(e.target.value)} aria-label="Artifact format" className="rounded border border-gray-300 px-1.5 py-0.5 text-xs">
              {["az", "powershell", "bicep", "terraform"].map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
            <button type="button" onClick={() => remediate.mutate()} className="rounded border bg-white px-2 py-1 text-xs hover:bg-gray-50">
              Generate
            </button>
          </div>
          <p className="mt-1 text-[11px] text-gray-600">
            This product never writes to Azure. It generates a script for you to read and run —
            with the rollback for every step in the same file.
          </p>
          {remediate.data?.note && <div className="mt-2 text-[11px] text-gray-600">{remediate.data.note}</div>}
          {bundle && (
            <>
              <div className="mt-2 text-[11px] text-gray-600">
                {bundle.action_count} action(s), ordered: group-derived access first, then broadest scope first.
              </div>
              <pre data-testid="remediation-script" className="mt-1 max-h-[50vh] overflow-auto rounded border bg-white p-2 text-[10px] leading-tight text-gray-800">
                {bundle.script}
              </pre>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export function ReviewsTab() {
  const connectionId = useIamConnectionId();
  const qc = useQueryClient();
  const [openId, setOpenId] = useState("");
  const [name, setName] = useState("");
  const [selectorIndex, setSelectorIndex] = useState(0);
  const [strategy, setStrategy] = useState("owner");

  const q = useQuery({
    queryKey: ["iam", "campaigns", connectionId],
    queryFn: () => api.iamCampaigns(connectionId),
  });
  const create = useMutation({
    mutationFn: () =>
      api.iamCreateCampaign(
        { name, selector: SELECTORS[selectorIndex].value, reviewer_strategy: strategy },
        connectionId,
      ),
    onSuccess: (r) => {
      setName("");
      qc.invalidateQueries({ queryKey: ["iam", "campaigns"] });
      setOpenId(r.campaign.id);
    },
  });

  if (openId) return <CampaignDetail id={openId} onBack={() => setOpenId("")} />;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b bg-white px-4 py-3">
        <div className="text-sm font-semibold text-gray-800">Access review campaigns</div>
        <p className="mt-1 text-[11px] text-gray-600">
          Entra Access Reviews cover directory roles and group membership. They do not cover Azure
          RBAC at resource scope, Key Vault access policies, classic administrators or bypass
          credentials — this does.
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-1">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Campaign name"
            aria-label="Campaign name"
            className="min-w-0 flex-1 rounded border border-gray-300 px-2 py-1 text-xs"
          />
          <select value={selectorIndex} onChange={(e) => setSelectorIndex(Number(e.target.value))} aria-label="What to certify" className="rounded border border-gray-300 px-1.5 py-1 text-xs">
            {SELECTORS.map((s, i) => <option key={s.label} value={i}>{s.label}</option>)}
          </select>
          <select value={strategy} onChange={(e) => setStrategy(e.target.value)} aria-label="Reviewer strategy" className="rounded border border-gray-300 px-1.5 py-1 text-xs">
            <option value="owner">Reviewed by the scope owner</option>
            <option value="manager">Reviewed by the principal's manager</option>
            <option value="self">Self-attestation (not certification)</option>
          </select>
          <button
            type="button"
            onClick={() => create.mutate()}
            disabled={!name.trim() || create.isPending}
            className="rounded border bg-white px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            Create
          </button>
        </div>
        {create.isError && <div className="mt-1 text-[11px] text-red-700">{(create.error as Error).message}</div>}
      </div>

      <div className="min-h-0 flex-1 space-y-2 overflow-auto p-3">
        {q.isLoading && <div className="text-sm text-gray-500">Loading…</div>}
        {q.data && q.data.campaigns.length === 0 && (
          <div className="rounded border bg-white p-3 text-xs text-gray-600">
            No campaigns yet. A campaign certifies a specific snapshot, so the answer to "who had
            this access, and who signed it off" stays available after the estate has moved on.
          </div>
        )}
        {q.data?.campaigns.map((c) => (
          <CampaignCard key={c.id} c={c} onOpen={setOpenId} />
        ))}
      </div>
    </div>
  );
}
