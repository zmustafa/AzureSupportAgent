/**
 * "Send to →" menu for a chat message's action row.
 *
 * Lists the configured ticketing connectors (ServiceNow / Jira / XSOAR), asks for an explicit
 * confirmation, then POSTs the WHOLE conversation (start to finish) as a rich ticket body and
 * shows the created ticket number (linked) inline.
 *
 * Self-contained: lazily fetches the connector list on first open and owns its own
 * confirm / busy / result state so the parent (ChatView) only passes the chat id.
 */
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type TicketConnector, type TicketResult } from "../api";
import { formatError } from "../utils/format";

type Stage =
  | { kind: "menu" }
  | { kind: "confirm"; conn: TicketConnector }
  | { kind: "sending"; conn: TicketConnector }
  | { kind: "done"; conn: TicketConnector; result: TicketResult }
  | { kind: "error"; conn: TicketConnector; message: string };

export function SendToTicketMenu({ chatId }: { chatId: string | null }) {
  const [open, setOpen] = useState(false);
  const [stage, setStage] = useState<Stage>({ kind: "menu" });
  const [copied, setCopied] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const connectorsQ = useQuery({
    queryKey: ["ticketConnectors"],
    queryFn: api.ticketConnectors,
    enabled: open,
    staleTime: 300_000,
  });
  const connectors = connectorsQ.data?.connectors ?? [];

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) close(); };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  function close() { setOpen(false); setStage({ kind: "menu" }); }

  async function send(conn: TicketConnector) {
    if (!chatId) return;
    setStage({ kind: "sending", conn });
    try {
      const result = await api.sendChatToTicket(chatId, conn.id);
      setStage({ kind: "done", conn, result });
    } catch (e) {
      setStage({ kind: "error", conn, message: formatError(e) });
    }
  }

  const ICON: Record<string, string> = { servicenow: "🟢", jira: "🔷", xsoar: "🛡️" };

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => { setOpen((o) => !o); setStage({ kind: "menu" }); }}
        disabled={!chatId}
        title="Send this whole conversation to a ticketing system"
        className="flex items-center gap-1 rounded-md px-1.5 py-1 text-xs text-gray-500 transition hover:bg-gray-100 hover:text-gray-700 disabled:opacity-50"
      >
        <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M3 10l14-6-6 14-2-6-6-2z" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        Send to
        <span className="text-gray-400">▾</span>
      </button>

      {open && (
        <div className="absolute bottom-full right-0 z-50 mb-1 w-72 overflow-hidden rounded-xl border bg-white shadow-xl">
          {stage.kind === "menu" && (
            <>
              <div className="border-b px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-gray-400">Send to ticket</div>
              {connectorsQ.isLoading ? (
                <div className="px-3 py-3 text-sm text-gray-500">Loading connectors…</div>
              ) : connectors.length === 0 ? (
                <div className="px-3 py-3 text-xs text-gray-500">
                  No ticketing connectors configured. Add a ServiceNow or Jira connector in <b>Settings → Connectors</b>.
                </div>
              ) : (
                <ul className="max-h-64 overflow-auto py-1">
                  {connectors.map((c) => (
                    <li key={c.id}>
                      <button
                        onClick={() => setStage({ kind: "confirm", conn: c })}
                        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-gray-700 hover:bg-brand/5"
                      >
                        <span>{ICON[c.type] ?? "🎫"}</span>
                        <span className="min-w-0 flex-1 truncate">{c.name}</span>
                        <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">{c.label}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}

          {stage.kind === "confirm" && (
            <div className="p-3">
              <div className="text-sm font-medium text-gray-800">Create a ticket?</div>
              <p className="mt-1 text-xs text-gray-600">
                The <b>entire conversation</b> (start to finish) will be sent to <b>{stage.conn.name}</b> ({stage.conn.label}) as a new ticket — included in the body <b>and</b> attached as a PDF transcript.
              </p>
              <div className="mt-3 flex justify-end gap-2">
                <button onClick={() => setStage({ kind: "menu" })} className="rounded border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">Back</button>
                <button onClick={() => void send(stage.conn)} className="rounded-lg bg-brand px-3 py-1 text-xs font-semibold text-white hover:bg-brand-dark">Create ticket</button>
              </div>
            </div>
          )}

          {stage.kind === "sending" && (
            <div className="flex items-center gap-2 p-3 text-sm text-gray-600">
              <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-brand" />
              Creating ticket in {stage.conn.name}…
            </div>
          )}

          {stage.kind === "done" && (
            <div className="p-3">
              <div className="flex items-center gap-1.5 text-sm font-medium text-emerald-700">
                <span>✓</span> Ticket created
              </div>
              <div className="mt-1 text-xs text-gray-600">
                {stage.result.number ? (
                  <span className="inline-flex items-center gap-1.5">
                    {stage.result.url ? (
                      <a href={stage.result.url} target="_blank" rel="noreferrer" className="font-mono font-medium text-brand hover:underline">{stage.result.number}</a>
                    ) : (
                      <span className="font-mono font-medium text-gray-800">{stage.result.number}</span>
                    )}
                    <button
                      onClick={() => { void navigator.clipboard.writeText(stage.result.number!); setCopied(true); setTimeout(() => setCopied(false), 1500); }}
                      title="Copy ticket number"
                      className="rounded border px-1.5 py-0.5 text-[10px] text-gray-500 hover:bg-gray-50"
                    >
                      {copied ? "✓ Copied" : "Copy"}
                    </button>
                  </span>
                ) : (
                  <span className="text-gray-500">{stage.result.detail || "Created."}</span>
                )}
                <span className="text-gray-400"> in {stage.conn.name}</span>
              </div>
              <div className="mt-1 text-[11px]">
                {stage.result.attached
                  ? <span className="text-emerald-600">📎 Chat PDF attached.</span>
                  : stage.result.attach_error
                    ? <span className="text-amber-600">⚠ PDF attach failed: {stage.result.attach_error}</span>
                    : null}
              </div>
              <div className="mt-3 flex justify-end">
                <button onClick={close} className="rounded border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">Done</button>
              </div>
            </div>
          )}

          {stage.kind === "error" && (
            <div className="p-3">
              <div className="text-sm font-medium text-red-700">Ticket creation failed</div>
              <p className="mt-1 break-words text-xs text-red-600">{stage.message}</p>
              <div className="mt-3 flex justify-end gap-2">
                <button onClick={() => setStage({ kind: "menu" })} className="rounded border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">Back</button>
                <button onClick={() => void send(stage.conn)} className="rounded-lg bg-gray-900 px-3 py-1 text-xs font-semibold text-white">Retry</button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
