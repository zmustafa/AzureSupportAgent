import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, apiBase, HttpError } from "../api";
import { useAuth } from "./AuthContext";

export default function LoginPage() {
  const { login, error } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  const { data: config } = useQuery({ queryKey: ["auth-config"], queryFn: api.authConfig });

  // Surface SSO errors passed back via ?error=… on the login redirect.
  const ssoError = (() => {
    const p = new URLSearchParams(window.location.search).get("error");
    if (!p) return null;
    const map: Record<string, string> = {
      sso_failed: "Single sign-on failed. Please try again or use a local account.",
      sso_state: "Single sign-on session expired. Please try again.",
      sso_denied: "Your account is not permitted to sign in.",
      saml_missing: "No SAML response received from the identity provider.",
      saml_failed: "SAML sign-on failed. Please try again.",
    };
    return map[p] ?? "Sign-on failed. Please try again.";
  })();

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setLocalError(null);
    try {
      await login(username.trim(), password);
    } catch (err) {
      if (err instanceof HttpError) setLocalError(err.detail);
      else setLocalError("Login failed.");
    } finally {
      setSubmitting(false);
    }
  };

  const startSso = (idpId: string, type: string) => {
    const path = type === "saml" ? `/auth/saml/${idpId}/login` : `/auth/oidc/${idpId}/login`;
    window.location.href = `${apiBase}${path}`;
  };

  const localEnabled = config?.local_login_enabled ?? true;
  const providers = config?.providers ?? [];

  return (
    <div className="flex min-h-full flex-col items-center justify-center gap-4 bg-slate-100 p-6">
      <div className="w-full max-w-sm rounded-xl border bg-white p-8 shadow-sm">
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <span className="text-3xl">🤖</span>
          <h1 className="text-xl font-semibold text-slate-800">Azure Support Agent</h1>
          <p className="text-sm text-slate-500">Sign in to continue</p>
        </div>

        {(localError || error || ssoError) && (
          <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {localError || error || ssoError}
          </div>
        )}

        {localEnabled && (
          <form onSubmit={onSubmit} className="flex flex-col gap-3">
            <label className="text-sm font-medium text-slate-700">
              Username or email
              <input
                className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-brand-dark focus:outline-none"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoFocus
                autoComplete="username"
              />
            </label>
            <label className="text-sm font-medium text-slate-700">
              Password
              <input
                type="password"
                className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-brand-dark focus:outline-none"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
            </label>
            <button
              type="submit"
              disabled={submitting || !username || !password}
              className="mt-2 rounded-md bg-brand-dark px-4 py-2 text-sm font-medium text-white hover:bg-brand-dark/90 disabled:opacity-50"
            >
              {submitting ? "Signing in…" : "Sign in"}
            </button>
          </form>
        )}

        {providers.length > 0 && (
          <div className="mt-5">
            {localEnabled && (
              <div className="mb-4 flex items-center gap-3 text-xs uppercase tracking-wide text-slate-400">
                <span className="h-px flex-1 bg-slate-200" />
                or
                <span className="h-px flex-1 bg-slate-200" />
              </div>
            )}
            <div className="flex flex-col gap-2">
              {providers.map((p) => (
                <button
                  key={p.id}
                  onClick={() => startSso(p.id, p.type)}
                  className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {!localEnabled && providers.length === 0 && (
          <p className="text-center text-sm text-slate-500">
            No sign-in methods are configured. Contact your administrator.
          </p>
        )}
      </div>

      {/* Trust strip — signal the enterprise security posture before sign-in. */}
      <div className="w-full max-w-sm text-center">
        <p className="text-[11px] leading-relaxed text-slate-400">
          🔒 Runs in your tenant · 👁️ read-only by default · ✅ approval-gated writes · 🧾 audited
        </p>
      </div>
    </div>
  );
}

/** Forced password-change screen shown when must_change_password is set. */
export function ForcePasswordChange() {
  const { user, refresh, logout } = useAuth();
  const [pw, setPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setErr(null);
  }, [pw, confirm]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (pw !== confirm) {
      setErr("Passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      // Forced reset: server does not require the current password in this state.
      await api.changePassword(pw);
      await refresh();
    } catch (e2) {
      setErr(e2 instanceof HttpError ? e2.detail : "Could not change password.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-full items-center justify-center bg-slate-100 p-6">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-xl border bg-white p-8 shadow-sm"
      >
        <h1 className="mb-1 text-lg font-semibold text-slate-800">Set a new password</h1>
        <p className="mb-5 text-sm text-slate-500">
          Welcome, {user?.display_name || user?.email}. For security, please choose a new
          password before continuing.
        </p>
        {err && (
          <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {err}
          </div>
        )}
        <label className="text-sm font-medium text-slate-700">
          New password
          <input
            type="password"
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-brand-dark focus:outline-none"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            autoFocus
          />
        </label>
        <label className="mt-3 block text-sm font-medium text-slate-700">
          Confirm password
          <input
            type="password"
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-brand-dark focus:outline-none"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </label>
        <button
          type="submit"
          disabled={busy || !pw || !confirm}
          className="mt-5 w-full rounded-md bg-brand-dark px-4 py-2 text-sm font-medium text-white hover:bg-brand-dark/90 disabled:opacity-50"
        >
          {busy ? "Saving…" : "Update password"}
        </button>
        <button
          type="button"
          onClick={() => void logout()}
          className="mt-2 w-full rounded-md px-4 py-2 text-sm text-slate-500 hover:bg-slate-50"
        >
          Sign out
        </button>
      </form>
    </div>
  );
}
