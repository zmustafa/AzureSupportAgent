import { useCallback, useMemo, useState } from "react";
import { api, type InvestigateTreeNode } from "../../api";
import { formatError } from "../../utils/format";
import { InvestigateLink } from "./InvestigateLink";

/**
 * The group membership tree, in both directions.
 *
 * An indented disclosure tree rather than a node graph, deliberately. The question is "who
 * is in this group, and how do they get in" — or, upward, "which groups reach this
 * principal" — and that is a list with structure. A list is scannable top to bottom,
 * searchable, virtualisable at 562 children, keyboard-navigable and pasteable into a
 * ticket. A force-directed canvas of 562 user nodes is a picture, not an answer, and this
 * tenant has groups that size.
 *
 * Lazy by construction: one Graph call per branch opened. Nested membership exists in no
 * cache we hold — both collectors resolve it transitively, which throws the intermediate
 * groups away — so every level is live, and eagerly walking would be thousands of calls for
 * a screen most readers open to look at one branch.
 */

const KIND_GLYPH: Record<string, string> = {
  user: "👤", guest: "🌐", group: "👥", servicePrincipal: "🤖", managedIdentity: "⚙️",
  directoryRole: "🛡️", administrativeUnit: "🏛️", device: "💻", unknown: "❔",
};

const KIND_LABEL: Record<string, string> = {
  user: "User", guest: "Guest", group: "Group", servicePrincipal: "Service principal",
  managedIdentity: "Managed identity", directoryRole: "Directory role",
  administrativeUnit: "Administrative unit", device: "Device",
};

/** How many children to render before folding the rest behind a "show all". */
const VISIBLE_CHILDREN = 50;

/** Kinds that are principals and therefore have a dossier of their own. */
const INVESTIGABLE = new Set(["user", "guest", "group", "servicePrincipal", "managedIdentity"]);

type Loaded = Record<string, InvestigateTreeNode[]>;

function Badge({ text, title, cls }: { text: string; title: string; cls: string }) {
  return <span title={title} className={`rounded border px-1 py-0.5 text-[10px] ${cls}`}>{text}</span>;
}

function Row({
  node, depth, ancestors, loaded, open, busy, failed, onToggle,
}: {
  node: InvestigateTreeNode;
  depth: number;
  /** Every group id on the path from the root to this node's parent, inclusive. */
  ancestors: string[];
  loaded: Loaded;
  open: Set<string>;
  busy: Set<string>;
  failed: Set<string>;
  onToggle: (id: string) => void;
}) {
  // A group that already appears above itself would otherwise expand for ever: the server
  // dedupes within one request, but a -> b -> a is two requests and it cannot see the path.
  // Rendering the loop is also the only way the reader learns the directory contains one.
  const isCycle = ancestors.includes(node.id);
  const isOpen = open.has(node.id) && !isCycle;
  const kids = loaded[node.id];
  const count = kids?.length;
  return (
    <div>
      <div
        data-testid="member-node"
        data-kind={node.kind}
        data-cycle={isCycle ? "true" : undefined}
        className="flex items-center gap-1.5 rounded px-1 py-0.5 text-xs hover:bg-gray-50"
        style={{ paddingLeft: `${depth * 16 + 4}px` }}
      >
        {node.expandable && !isCycle ? (
          <button
            type="button"
            onClick={() => onToggle(node.id)}
            aria-expanded={isOpen}
            aria-label={`${isOpen ? "Collapse" : "Expand"} ${node.display_name || node.id}`}
            data-testid="member-toggle"
            className="w-4 shrink-0 text-gray-400 hover:text-brand"
          >
            {busy.has(node.id) ? "…" : isOpen ? "▾" : "▸"}
          </button>
        ) : (
          <span className="w-4 shrink-0">{isCycle ? "↺" : ""}</span>
        )}
        <span aria-hidden="true">{KIND_GLYPH[node.kind] ?? KIND_GLYPH.unknown}</span>
        <span className="truncate font-medium text-gray-800" title={node.display_name}>
          {node.display_name || node.id}
        </span>
        {node.upn && <span className="truncate text-[11px] text-gray-400">{node.upn}</span>}
        {isCycle && (
          <Badge text="already shown above"
                 title="This group is nested inside itself somewhere up this branch. The loop is real — it is not expanded again."
                 cls="border-violet-200 bg-violet-50 text-violet-800" />
        )}
        {node.enabled === false && (
          <Badge text="disabled" title="This account is disabled but still a member."
                 cls="border-gray-200 bg-gray-100 text-gray-600" />
        )}
        {node.expandable && !isCycle && count !== undefined && (
          <span className="shrink-0 rounded bg-gray-100 px-1 text-[10px] tabular-nums text-gray-600">
            {count}
          </span>
        )}
        {node.kind === "directoryRole" && (
          <Badge text="directory role"
                 title="Held through this membership. A directory role reached via a group is the escalation path an access review is looking for."
                 cls="border-rose-200 bg-rose-50 text-rose-800" />
        )}
        {failed.has(node.id) && (
          <Badge text="unreadable" title="This branch could not be read — that is not the same as it being empty."
                 cls="border-amber-200 bg-amber-50 text-amber-800" />
        )}
        {/* A directory role or administrative unit is not a principal — a dossier link to
            one resolves to nothing, which reads as "deleted" and is a lie. */}
        {INVESTIGABLE.has(node.kind) && (
          <span className="ml-auto shrink-0">
            <InvestigateLink principalId={node.id} label={node.display_name || node.id} />
          </span>
        )}
      </div>
      {isOpen && kids && <Children nodes={kids} depth={depth + 1} ancestors={[...ancestors, node.id]}
                                   loaded={loaded} open={open}
                                   busy={busy} failed={failed} onToggle={onToggle} />}
      {isOpen && kids && kids.length === 0 && !failed.has(node.id) && (
        <div className="py-0.5 text-[11px] text-gray-400" style={{ paddingLeft: `${(depth + 1) * 16 + 24}px` }}>
          No members.
        </div>
      )}
    </div>
  );
}

function Children({
  nodes, depth, ancestors, loaded, open, busy, failed, onToggle,
}: {
  nodes: InvestigateTreeNode[]; depth: number; ancestors: string[]; loaded: Loaded;
  open: Set<string>; busy: Set<string>; failed: Set<string>; onToggle: (id: string) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const shown = showAll ? nodes : nodes.slice(0, VISIBLE_CHILDREN);
  return (
    <div className="border-l border-gray-100">
      {shown.map((n) => (
        <Row key={n.id} node={n} depth={depth} ancestors={ancestors} loaded={loaded} open={open}
             busy={busy} failed={failed} onToggle={onToggle} />
      ))}
      {!showAll && nodes.length > VISIBLE_CHILDREN && (
        <button
          type="button"
          onClick={() => setShowAll(true)}
          data-testid="member-show-all"
          className="ml-6 py-0.5 text-[11px] text-brand hover:underline"
          style={{ paddingLeft: `${depth * 16}px` }}
        >
          show all {nodes.length}
        </button>
      )}
    </div>
  );
}

export function MembersTree({
  principalId, rootName, connectionId, mode = "both", rootKind = "group",
}: {
  principalId: string;
  rootName: string;
  connectionId: string;
  /** "up" renders only the upward view — the memberships section, where "who is inside
   *  this" is not a question that applies to the subject. */
  mode?: "both" | "up";
  rootKind?: string;
}) {
  const [direction, setDirection] = useState<"down" | "up">(mode === "up" ? "up" : "down");
  const [transitive, setTransitive] = useState(false);
  const [loaded, setLoaded] = useState<Loaded>({});
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [failed, setFailed] = useState<Set<string>>(new Set());
  const [notes, setNotes] = useState<string[]>([]);
  const [err, setErr] = useState("");
  const [started, setStarted] = useState(false);

  const load = useCallback(async (ids: string[], dir: "down" | "up", trans: boolean) => {
    setBusy((b) => new Set([...b, ...ids]));
    setErr("");
    try {
      const r = await api.entraInvestigateMembers(
        principalId, { expand: ids, direction: dir, transitive: trans && dir === "up" },
        connectionId || null);
      setLoaded((prev) => ({ ...prev, ...r.nodes }));
      setNotes(r.notes ?? []);
      // A branch the server answered with nothing AND named in a note is unreadable, not
      // empty. Keeping those apart is the whole point of the note.
      const bad = new Set<string>();
      for (const n of r.notes ?? []) {
        const id = n.split(":")[0]?.trim();
        if (id && (r.nodes ?? {})[id]?.length === 0) bad.add(id);
      }
      setFailed((f) => new Set([...f, ...bad]));
    } catch (e) {
      setErr(formatError(e));
    } finally {
      setBusy((b) => { const n = new Set(b); ids.forEach((i) => n.delete(i)); return n; });
    }
  }, [principalId, connectionId]);

  const begin = async (dir: "down" | "up", trans = transitive) => {
    setDirection(dir);
    setTransitive(trans);
    setStarted(true);
    setLoaded({}); setOpen(new Set()); setFailed(new Set());
    await load([], dir, trans);
  };

  const onToggle = useCallback((id: string) => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) { next.delete(id); return next; }
      next.add(id);
      return next;
    });
    // A branch below the root is always a group and always read one direct level at a
    // time — the transitive shortcut applies to the root only.
    if (!loaded[id]) void load([id], direction, false);
  }, [loaded, load, direction]);

  const roots = loaded[principalId] ?? [];
  const stats = useMemo(() => {
    const out: Record<string, number> = {};
    for (const n of roots) out[n.kind] = (out[n.kind] ?? 0) + 1;
    return out;
  }, [roots]);

  // A group dossier renders BOTH trees. Sharing one test id would make every selector
  // ambiguous and silently target whichever happened to be first in the DOM.
  const tid = mode === "up" ? "memberships" : "members";

  return (
    <div className="rounded border bg-gray-50/60 p-2" data-testid={`${tid}-tree`}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        {mode === "both" && (
          <button
            type="button"
            onClick={() => void begin("down")}
            disabled={busy.size > 0}
            data-testid="members-load-down"
            className="rounded bg-brand px-2 py-1 text-[11px] font-medium text-white disabled:opacity-50"
          >
            {started && direction === "down" ? "Reload members" : "Show member tree"}
          </button>
        )}
        <button
          type="button"
          onClick={() => void begin("up")}
          disabled={busy.size > 0}
          data-testid={`${tid}-load-up`}
          title={mode === "up"
            ? "Read every group this principal belongs to, live from the directory — not just the ones that grant something."
            : "Which groups this group is itself a member of — where it inherits access from."}
          className={mode === "up"
            ? "rounded bg-brand px-2 py-1 text-[11px] font-medium text-white disabled:opacity-50"
            : "rounded border bg-white px-2 py-1 text-[11px] text-gray-700 hover:bg-gray-50 disabled:opacity-50"}
        >
          {mode === "up"
            ? (started ? "Reload from directory" : "Read every group live")
            : "Show parent groups"}
        </button>
        {mode === "up" && (
          <label className="flex items-center gap-1 text-[11px] text-gray-600"
                 title="Include groups reached through nesting. Direct membership answers 'where was this principal added'; transitive answers 'whose access reaches it'.">
            <input
              type="checkbox"
              data-testid="memberships-transitive"
              checked={transitive}
              disabled={busy.size > 0}
              onChange={(e) => { const v = e.target.checked; setTransitive(v); if (started) void begin("up", v); }}
            />
            include nested
          </label>
        )}
        <span className="text-[11px] text-gray-500">
          Read live from the directory, one level at a time. Nested membership is not cached.
        </span>
      </div>

      {err && <div className="rounded border border-rose-200 bg-rose-50 p-2 text-[11px] text-rose-800">{err}</div>}

      {started && !err && (
        <>
          <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] text-gray-600">
            <span className="font-medium">
              {direction === "down"
                ? `${roots.length} direct member(s)`
                : `Member of ${roots.length} ${transitive ? "group(s), nesting included" : "group(s) directly"}`}
            </span>
            {Object.entries(stats).map(([k, n]) => (
              <span key={k} className="rounded bg-white px-1.5 py-0.5 text-[10px] text-gray-600">
                {n} {KIND_LABEL[k] ?? k}
              </span>
            ))}
          </div>

          {notes.length > 0 && (
            <ul className="mb-1 list-disc space-y-0.5 rounded border border-amber-200 bg-amber-50 px-2 py-1 pl-6 text-[10px] text-amber-900">
              {notes.map((n, i) => <li key={i}>{n}</li>)}
            </ul>
          )}

          {roots.length === 0 && !busy.size ? (
            <div className="text-[11px] text-gray-500">
              {direction === "down"
                ? "This group has no direct members."
                : mode === "up"
                  ? "The directory returned no group for this principal."
                  : "This group is not nested inside any other group."}
            </div>
          ) : (
            <div className="max-h-96 overflow-auto rounded border bg-white p-1">
              <div className="flex items-center gap-1.5 px-1 py-0.5 text-xs font-semibold text-gray-800">
                <span className="w-4" />
                <span aria-hidden="true">{KIND_GLYPH[rootKind] ?? KIND_GLYPH.unknown}</span>
                <span className="truncate">{rootName}</span>
              </div>
              <Children nodes={roots} depth={1} ancestors={[principalId]} loaded={loaded}
                        open={open} busy={busy}
                        failed={failed} onToggle={onToggle} />
            </div>
          )}
        </>
      )}
    </div>
  );
}
