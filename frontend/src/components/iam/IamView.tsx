/** IAM — Access Review shell.
 *
 *  Owns the header, the connection scope, the tab bar and the empty/loading states; each tab's
 *  rendering lives in its own `Iam*.tsx` file. Renamed from the former /rbac screen: it covers
 *  access models that are not RBAC (Key Vault access policies, classic administrators, PIM).
 *
 *  Layout note: this root MUST stay `flex h-full min-h-0 flex-col`. The host in ChatView is a
 *  block with `overflow-auto`, so a `flex-1` root resolves to nothing, grows to full content
 *  height and scrolls the pinned header away.
 */
import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../../api";
import { formatError } from "../../utils/format";
import { CONNECTION_KEY, migrateConnectionKeys, usePersistedState } from "../../utils/persistedState";
import { Skeleton } from "../../utils/perf";
import { IAM_NAV, type IamTab } from "../navConfig";
import { ConnectionScopePicker } from "../ConnectionScopePicker";
import { AccessGrid } from "./IamAccessGrid";
import { DiagnosticsTab } from "./IamDiagnostics";
import { EffectiveTab } from "./IamEffective";
import { IamFlowTab } from "./IamFlowTab";
import { EscalationTab } from "./IamEscalation";
import { BypassTab } from "./IamBypass";
import { CompareTab } from "./IamCompare";
import { LeastPrivilegeTab } from "./IamLeastPrivilege";
import { SimulatorTab } from "./IamSimulator";
import { ReviewsTab } from "./IamReviews";
import { FindingsTab } from "./IamFindings";
import { ScannersTab } from "./IamScanners";
import { InsightsTab } from "./IamInsights";
import { OverviewTab } from "./IamOverview";
import { PimTab } from "./IamPim";
import { RolesTab } from "./IamRoles";
import { ScopesTab } from "./IamScopes";
import { IAM_QUERY_KEYS, IamConnectionContext, RefreshConsole, useIamRefresh } from "./IamShared";

// Renamed from /rbac, and later folded into the SHARED connection selection: a tenant switch is
// a statement about the session, not about one page. Losing the stored value silently falls back
// to the DEFAULT connection, so the reader could end up looking at another tenant without
// noticing - hence the migration rather than a reset.
migrateConnectionKeys();

export function IamPanel({ tab = "overview" }: { tab?: IamTab }) {
  // The connection lives here so the provider can wrap everything that reads it.
  const [connectionId, setConnectionId] = usePersistedState(CONNECTION_KEY, "");
  return (
    <IamConnectionContext.Provider value={connectionId}>
      <IamPanelBody tab={tab} connectionId={connectionId} setConnectionId={setConnectionId} />
    </IamConnectionContext.Provider>
  );
}

function IamPanelBody({
  tab,
  connectionId,
  setConnectionId,
}: {
  tab: IamTab;
  connectionId: string;
  setConnectionId: (v: string) => void;
}) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [seeding, setSeeding] = useState(false);
  const [purging, setPurging] = useState(false);
  const [err, setErr] = useState("");
  const refreshCtl = useIamRefresh();

  const overviewQ = useQuery({ queryKey: ["iam", "overview", connectionId], queryFn: () => api.iamOverview(connectionId), staleTime: 5 * 60 * 1000 });

  // Reconnect to any in-flight refresh job on mount (the job survives navigation).
  useEffect(() => {
    let cancelled = false;
    api.iamJob({ mode: "all", connection_id: connectionId || null }).then((r) => {
      if (!cancelled && r.job?.status === "running") {
        refreshCtl.refreshAll();
      }
    }).catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionId]);

  const setTab = (v: IamTab) => navigate(v === "overview" ? "/iam" : `/iam/${v}`);

  async function seedDemo() {
    setSeeding(true);
    setErr("");
    try {
      await api.iamDemoSeed();
      for (const k of IAM_QUERY_KEYS) {
        qc.invalidateQueries({ queryKey: ["iam", k] });
      }
    } catch (e) {
      setErr(formatError(e));
    } finally {
      setSeeding(false);
    }
  }

  async function purgeDemo() {
    if (!window.confirm("Remove the IAM demo dataset? This only clears the synthetic demo data; re-seed any time.")) return;
    setPurging(true);
    setErr("");
    try {
      await api.iamDemoPurge();
      for (const k of [...IAM_QUERY_KEYS, "scope-tree"]) {
        qc.invalidateQueries({ queryKey: ["iam", k] });
      }
    } catch (e) {
      setErr(formatError(e));
    } finally {
      setPurging(false);
    }
  }

  const data = overviewQ.data;

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-gray-50">
      {/* Header + tab bar */}
      <div className="border-b bg-white px-4 pt-3">
        <div className="mb-2 flex items-center gap-2">
          <h1 className="text-lg font-semibold text-gray-900">IAM — Access Review</h1>
          <span className="text-xs text-gray-500">Who can access what across Azure RBAC, Entra roles, groups &amp; ownership</span>
          <div className="ml-auto">
            <ConnectionScopePicker value={connectionId} onChange={setConnectionId} />
          </div>
        </div>
        {/* Horizontal scroll, never wrap: Entra's 9 labels wrapped into three ragged rows at
            1440px. Sixteen tabs is why everything here is tight — at the default
            `px-3 text-sm` plus full-size emoji the row measured 2021px against 1352px of bar,
            hiding five tabs behind a scrollbar. The glyph is rendered at 11px rather than at
            label size, and the padding is what pays for the 13px label: at `px-1.5` the row is
            1376px and overflows again. 14px does not fit at any padding. */}
        <div className="flex items-center overflow-x-auto">
          {IAM_NAV.map(({ id, label, icon }) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              className={`shrink-0 whitespace-nowrap rounded-t-lg px-1 py-1.5 text-[13px] font-medium ${
                tab === id ? "border-b-2 border-brand text-brand" : "text-gray-500 hover:text-gray-700"
              }`}
            >
              <span aria-hidden="true" className="mr-0.5 text-[11px]">{icon}</span>
              {label}
            </button>
          ))}
        </div>
      </div>

      {err && <div className="border-b bg-red-50 px-4 py-2 text-sm text-red-700">{err}</div>}

      {overviewQ.isLoading ? (
        <div className="p-6"><Skeleton rows={8} /></div>
      ) : data && data.never_loaded && tab === "overview" ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
          <div className="text-4xl">🛡️</div>
          <div className="text-lg font-semibold text-gray-800">No access scan loaded yet</div>
          <p className="max-w-md text-sm text-gray-500">
            Run an access scan to inventory who can access what, or load the demo dataset to explore the
            review without an Azure connection.
          </p>
          <div className="flex gap-2">
            <button onClick={refreshCtl.refreshAll} disabled={refreshCtl.isBusy} className="rounded bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-dark disabled:opacity-50">
              {refreshCtl.isBusy ? "Scanning…" : "↻ Run access scan"}
            </button>
            <button onClick={seedDemo} disabled={seeding} className="rounded border px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50">
              {seeding ? "Seeding…" : "🎬 Seed demo data"}
            </button>
          </div>
          <div className="mt-2 w-full max-w-lg text-left">
            <RefreshConsole ctl={refreshCtl} lines={6} />
          </div>
        </div>
      ) : !data ? (
        <div className="p-6 text-sm text-gray-500">No data.</div>
      ) : tab === "overview" ? (
        <OverviewTab data={data} refreshCtl={refreshCtl} onPurgeDemo={purgeDemo} purging={purging} />
      ) : tab === "findings" ? (
        <FindingsTab />
      ) : tab === "scanners" ? (
        <ScannersTab />
      ) : tab === "effective" || tab === "privileged" ? (
        // One grid, one lens control. `/iam/privileged` used to be a separate tab whose only
        // difference was a server-side `roleIsPrivileged` filter — the same filter the grid's
        // own checkbox applies — so it now seeds that checkbox instead. The route is kept so
        // existing links keep landing on the view they promised.
        <AccessGrid tab="effective" initialPrivOnly={tab === "privileged"} />
      ) : tab === "evaluate" ? (
        <EffectiveTab />
      ) : tab === "accessmap" ? (
        <IamFlowTab />
      ) : tab === "escalation" ? (
        <EscalationTab />
      ) : tab === "bypass" ? (
        <BypassTab />
      ) : tab === "leastprivilege" ? (
        <LeastPrivilegeTab />
      ) : tab === "simulator" ? (
        <SimulatorTab />
      ) : tab === "compare" ? (
        <CompareTab />
      ) : tab === "reviews" ? (
        <ReviewsTab />
      ) : tab === "pim" ? (
        <PimTab />
      ) : tab === "scopes" ? (
        <ScopesTab refreshCtl={refreshCtl} />
      ) : tab === "roles" ? (
        <RolesTab />
      ) : tab === "insights" ? (
        <InsightsTab />
      ) : (
        <DiagnosticsTab />
      )}
    </div>
  );
}

