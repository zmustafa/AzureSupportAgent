import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "./AuthContext";
import { APP_VERSION_DISPLAY } from "../version";
import { DOCS_LINKS, GLOSSARY, SHORTCUTS, TRUST_POINTS } from "../help/content";

type ModalKind = null | "glossary" | "shortcuts" | "trust" | "about";

/**
 * Header Help (?) menu — the single discoverable entry point for understanding the app:
 * Glossary, keyboard shortcuts, Trust & Security (with live status for admins), links to the
 * docs, and About / version. Self-contained: owns its dropdown + dialogs.
 */
export function HelpMenu() {
  const [openMenu, setOpenMenu] = useState(false);
  const [modal, setModal] = useState<ModalKind>(null);
  const ref = useRef<HTMLDivElement>(null);

  // Open the menu with "?" when not typing in a field.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "?" && !isTypingTarget(e.target)) {
        e.preventDefault();
        setOpenMenu((v) => !v);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpenMenu(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  function pick(m: ModalKind) {
    setModal(m);
    setOpenMenu(false);
  }

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpenMenu((v) => !v)}
        title="Help (?)"
        aria-label="Help"
        className="flex h-7 w-7 items-center justify-center rounded-full border border-white/30 text-sm font-semibold hover:bg-white/10"
      >
        ?
      </button>
      {openMenu && (
        <div className="absolute right-0 z-50 mt-2 w-56 overflow-hidden rounded-xl border border-gray-200 bg-white py-1 text-sm text-gray-700 shadow-2xl">
          <MenuItem icon="📖" label="Glossary" onClick={() => pick("glossary")} />
          <MenuItem icon="⌨️" label="Keyboard shortcuts" onClick={() => pick("shortcuts")} />
          <MenuItem icon="🔒" label="Trust & Security" onClick={() => pick("trust")} />
          <div className="my-1 border-t" />
          <MenuLink icon="🚀" label="Getting started" href={DOCS_LINKS.userGuide} />
          <MenuLink icon="📚" label="Documentation" href={DOCS_LINKS.index} />
          <div className="my-1 border-t" />
          <MenuItem icon="ℹ️" label="About" onClick={() => pick("about")} />
        </div>
      )}

      {modal === "glossary" && <GlossaryModal onClose={() => setModal(null)} />}
      {modal === "shortcuts" && <ShortcutsModal onClose={() => setModal(null)} />}
      {modal === "trust" && <TrustModal onClose={() => setModal(null)} />}
      {modal === "about" && <AboutModal onClose={() => setModal(null)} />}
    </div>
  );
}

function isTypingTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable;
}

function MenuItem({ icon, label, onClick }: { icon: string; label: string; onClick: () => void }) {
  return (
    <button onClick={onClick} className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left hover:bg-gray-50">
      <span aria-hidden>{icon}</span>
      <span>{label}</span>
    </button>
  );
}

function MenuLink({ icon, label, href }: { icon: string; label: string; href: string }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" className="flex w-full items-center gap-2.5 px-3 py-1.5 hover:bg-gray-50">
      <span aria-hidden>{icon}</span>
      <span>{label}</span>
      <span className="ml-auto text-[10px] text-gray-300">↗</span>
    </a>
  );
}

// ---- Shared modal shell ---------------------------------------------------------
function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: React.ReactNode; wide?: boolean }) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/40 px-4 py-6 backdrop-blur-[1px]" onClick={onClose}>
      <div className={`flex max-h-[82vh] w-full ${wide ? "max-w-2xl" : "max-w-lg"} flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-2xl`} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-5 py-3">
          <h2 className="text-base font-semibold text-gray-900">{title}</h2>
          <button onClick={onClose} className="rounded p-1 text-gray-400 hover:bg-gray-100" aria-label="Close">✕</button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4 text-sm text-gray-700">{children}</div>
      </div>
    </div>
  );
}

function GlossaryModal({ onClose }: { onClose: () => void }) {
  const [q, setQ] = useState("");
  const terms = GLOSSARY.filter((t) => {
    const s = q.trim().toLowerCase();
    return !s || `${t.term} ${t.short} ${t.long}`.toLowerCase().includes(s);
  });
  return (
    <Modal title="Glossary" onClose={onClose} wide>
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search terms…"
        className="mb-3 w-full rounded-lg border px-3 py-1.5 text-sm outline-none focus:border-brand/40"
        autoFocus
      />
      <dl className="space-y-3">
        {terms.map((t) => (
          <div key={t.term} className="rounded-lg border bg-gray-50/60 p-3">
            <dt className="text-sm font-semibold text-gray-900">{t.term}</dt>
            <dd className="mt-0.5 text-xs text-gray-600">{t.long}</dd>
          </div>
        ))}
        {terms.length === 0 && <p className="py-6 text-center text-gray-400">No matching terms.</p>}
      </dl>
      <p className="mt-3 text-xs text-gray-400">
        Full reference: <a href={DOCS_LINKS.concepts} target="_blank" rel="noreferrer" className="text-brand hover:underline">Concepts &amp; Glossary →</a>
      </p>
    </Modal>
  );
}

function ShortcutsModal({ onClose }: { onClose: () => void }) {
  return (
    <Modal title="Keyboard shortcuts" onClose={onClose}>
      <table className="w-full text-sm">
        <tbody>
          {SHORTCUTS.map((s) => (
            <tr key={s.keys} className="border-b last:border-0">
              <td className="py-2 pr-4"><kbd className="rounded border bg-gray-50 px-2 py-0.5 text-xs">{s.keys}</kbd></td>
              <td className="py-2 text-gray-600">{s.action}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Modal>
  );
}

function TrustModal({ onClose }: { onClose: () => void }) {
  const { isAdmin } = useAuth();
  const statusQ = useQuery({ queryKey: ["metaStatus"], queryFn: api.metaStatus, enabled: isAdmin, retry: false });
  return (
    <Modal title="Trust & Security" onClose={onClose} wide>
      <p className="mb-3 text-xs text-gray-500">
        Azure Support Agent is built to run safely inside your own tenant. The posture below is enforced by default.
      </p>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {TRUST_POINTS.map((p) => (
          <div key={p.title} className="rounded-lg border bg-gray-50/60 p-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-gray-800"><span aria-hidden>{p.icon}</span>{p.title}</div>
            <p className="mt-0.5 text-xs text-gray-600">{p.body}</p>
          </div>
        ))}
      </div>
      {isAdmin && (
        <div className="mt-4">
          <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-gray-400">System status</div>
          {statusQ.isLoading ? (
            <p className="text-xs text-gray-400">Checking…</p>
          ) : statusQ.isError || !statusQ.data ? (
            <p className="text-xs text-gray-400">Status unavailable.</p>
          ) : (
            <div className="space-y-1">
              {Object.entries(statusQ.data.checks).map(([k, c]) => (
                <div key={k} className="flex items-center gap-2 text-sm">
                  <span className={c.ok ? "text-green-600" : "text-amber-600"}>{c.ok ? "●" : "○"}</span>
                  <span className="text-gray-700">{c.label}</span>
                  {typeof c.count === "number" && <span className="text-xs text-gray-400">({c.count})</span>}
                </div>
              ))}
              <div className="pt-1 text-[11px] text-gray-400">
                {statusQ.data.environment} · uptime {Math.floor(statusQ.data.uptime_seconds / 60)}m
              </div>
            </div>
          )}
        </div>
      )}
      <p className="mt-3 text-xs text-gray-400">
        More: <a href={DOCS_LINKS.concepts} target="_blank" rel="noreferrer" className="text-brand hover:underline">Security &amp; access model →</a>
      </p>
    </Modal>
  );
}

function AboutModal({ onClose }: { onClose: () => void }) {
  const metaQ = useQuery({ queryKey: ["meta"], queryFn: api.meta, retry: false });
  return (
    <Modal title="About Azure Support Agent" onClose={onClose}>
      <div className="flex items-center gap-3">
        <span className="text-3xl" aria-hidden>🤖</span>
        <div>
          <div className="text-base font-semibold text-gray-900">Azure Support Agent</div>
          <div className="text-xs text-gray-500">An AI operations workbench that runs in your tenant.</div>
        </div>
      </div>
      <dl className="mt-4 space-y-1.5 text-sm">
        <Row label="Version" value={APP_VERSION_DISPLAY} />
        <Row label="Environment" value={metaQ.data?.environment ?? "—"} />
      </dl>
      <p className="mt-4 text-xs text-gray-400">
        <a href={DOCS_LINKS.index} target="_blank" rel="noreferrer" className="text-brand hover:underline">Documentation</a>
        {" · "}
        <a href={DOCS_LINKS.userGuide} target="_blank" rel="noreferrer" className="text-brand hover:underline">User Guide</a>
      </p>
    </Modal>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b py-1 last:border-0">
      <dt className="text-gray-500">{label}</dt>
      <dd className="font-medium text-gray-800">{value}</dd>
    </div>
  );
}
