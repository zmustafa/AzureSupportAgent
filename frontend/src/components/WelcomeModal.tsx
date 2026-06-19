import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "./AuthContext";
import { DOCS_LINKS } from "../help/content";

const SEEN_PREFIX = "azsup.welcome.seen.";

/**
 * First-run Welcome — the front door that turns a cold, empty app into an instantly useful
 * one. Two paths: explore a synthetic sample tenant (no Azure needed) or connect your own.
 * Shown once per user; re-openable from Help. Mounted at the app root.
 */
export function WelcomeModal() {
  const { user, isAdmin } = useAuth();
  const navigate = useNavigate();
  const seenKey = SEEN_PREFIX + (user?.subject ?? user?.email ?? "anon");
  const [open, setOpen] = useState(() => localStorage.getItem(seenKey) !== "1");
  const [seeding, setSeeding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const demoQ = useQuery({ queryKey: ["demoStatus"], queryFn: api.demoStatus, enabled: open && isAdmin, retry: false });
  const demoLoaded = demoQ.data?.loaded ?? false;

  function dismiss() {
    localStorage.setItem(seenKey, "1");
    setOpen(false);
  }

  async function exploreDemo() {
    if (seeding) return;
    setError(null);
    if (demoLoaded) {
      dismiss();
      navigate("/dashboard");
      return;
    }
    setSeeding(true);
    try {
      await api.seedDemoData();
      dismiss();
      navigate("/dashboard");
      // Reload so every cached query picks up the freshly seeded sample tenant.
      window.location.reload();
    } catch (e) {
      setError((e as Error)?.message || "Could not load demo data.");
    } finally {
      setSeeding(false);
    }
  }

  function connectAzure() {
    dismiss();
    navigate(isAdmin ? "/admin/providers" : "/dashboard");
  }

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[65] flex items-center justify-center bg-black/50 px-4 py-6 backdrop-blur-[1px]">
      <div className="w-full max-w-lg overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-2xl">
        <div className="bg-gradient-to-br from-brand/10 to-violet-50 px-6 py-5">
          <div className="flex items-center gap-3">
            <span className="text-3xl" aria-hidden>🤖</span>
            <div>
              <h2 className="text-lg font-bold text-gray-900">Welcome to Azure Support Agent</h2>
              <p className="text-sm text-gray-600">An AI operations workbench that runs in your own tenant.</p>
            </div>
          </div>
        </div>

        <div className="space-y-3 px-6 py-5">
          <p className="text-sm text-gray-600">Pick how you'd like to start:</p>

          {/* Explore demo data */}
          {isAdmin ? (
            <button
              onClick={() => void exploreDemo()}
              disabled={seeding}
              className="flex w-full items-start gap-3 rounded-xl border border-brand/30 bg-brand/5 p-4 text-left transition hover:border-brand/50 hover:bg-brand/10 disabled:opacity-60"
            >
              <span className="text-2xl" aria-hidden>🎬</span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-semibold text-gray-900">
                  {seeding ? "Loading sample tenant…" : demoLoaded ? "Explore the sample tenant" : "Explore a sample tenant"}
                </span>
                <span className="mt-0.5 block text-xs text-gray-600">
                  Try every feature instantly with synthetic data — no Azure connection required. Remove it anytime from Settings → Demo Data.
                </span>
              </span>
            </button>
          ) : (
            <div className="flex items-start gap-3 rounded-xl border bg-gray-50 p-4">
              <span className="text-2xl" aria-hidden>🎬</span>
              <span className="text-xs text-gray-600">Ask an admin to load demo data, or to connect your Azure tenant, to get started.</span>
            </div>
          )}

          {/* Connect Azure */}
          <button
            onClick={connectAzure}
            className="flex w-full items-start gap-3 rounded-xl border p-4 text-left transition hover:border-gray-300 hover:bg-gray-50"
          >
            <span className="text-2xl" aria-hidden>🏢</span>
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-semibold text-gray-900">Connect your Azure</span>
              <span className="mt-0.5 block text-xs text-gray-600">
                Set up an AI provider and an Azure tenant connection. Access is read-only by default.
              </span>
            </span>
          </button>

          {error && <div className="rounded-lg border border-red-200 bg-red-50 p-2 text-xs text-red-700">{error}</div>}

          <div className="flex items-center justify-between pt-1">
            <a href={DOCS_LINKS.userGuide} target="_blank" rel="noreferrer" className="text-xs text-brand hover:underline">Read the User Guide →</a>
            <button onClick={dismiss} className="text-xs text-gray-400 hover:text-gray-600">Skip for now</button>
          </div>
        </div>
      </div>
    </div>
  );
}
