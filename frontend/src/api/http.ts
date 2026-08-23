// Shared fetch layer for the API client. Imports nothing from ./endpoints — keeping this
// module dependency-free is what lets endpoint modules be tree-shaken independently.

export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:35001/api";

/** Absolute API origin, e.g. for SSO redirects (full-page navigations). */
export const apiBase = API_BASE;

export class HttpError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail || `${status}`);
    this.status = status;
    this.detail = detail;
  }
}

export async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    // API responses are dynamic and must never come from the browser HTTP cache
    // (e.g. a stale /me after switching roles). React Query handles app-level caching.
    cache: "no-store",
    ...init,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body?.detail)) detail = body.detail.map((item: unknown) => typeof item === "string" ? item : JSON.stringify(item)).join("\n");
      else if (body?.detail && typeof body.detail === "object") detail = typeof body.detail.message === "string" ? body.detail.message : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new HttpError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export async function httpBlob(path: string, init?: RequestInit): Promise<Blob> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    cache: "no-store",
    ...init,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new HttpError(res.status, detail);
  }
  return res.blob();
}

/** POST a multipart form (file upload). Lets the browser set the multipart boundary. */
export async function httpUpload<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    body: form,
    credentials: "include",
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON */
    }
    throw new HttpError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

/** Trigger a browser download of a Blob with a filename. */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
