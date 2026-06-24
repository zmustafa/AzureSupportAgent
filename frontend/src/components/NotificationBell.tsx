import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api, type AppNotification } from "../api";
import { formatRelativeFromNow } from "../utils/format";
import { notificationLink, SEVERITY_DOT as SEV_DOT } from "../utils/notificationLink";

export function NotificationBell({ collapsed }: { collapsed?: boolean }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Poll unread count every 60s (long interval keeps the badge fresh enough without
  // chatter). We also force an immediate refetch when the tab regains visibility, so
  // returning from another window updates the badge instantly without paying for fast
  // background polling.
  const countQ = useQuery({
    queryKey: ["notificationsUnread"],
    queryFn: api.notificationsUnread,
    refetchInterval: 60_000,
    // Don't poll the unread COUNT while the tab is hidden — the visibility handler below
    // forces a fresh fetch the instant the tab is shown again, so the badge is still
    // up to date on return without paying for background polling.
    refetchIntervalInBackground: false,
    staleTime: 30_000,
  });
  const listQ = useQuery({
    queryKey: ["notificationsList"],
    queryFn: () => api.notifications(false),
    enabled: open,
  });

  useEffect(() => {
    function onVisible() {
      if (document.visibilityState === "visible") {
        qc.invalidateQueries({ queryKey: ["notificationsUnread"] });
      }
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [qc]);

  const unread = countQ.data?.count ?? 0;
  const notes = listQ.data?.notifications ?? [];

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    if (open) document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  async function markRead(id: string) {
    await api.markNotificationRead(id);
    qc.invalidateQueries({ queryKey: ["notificationsUnread"] });
    qc.invalidateQueries({ queryKey: ["notificationsList"] });
  }
  async function markAll() {
    await api.markAllNotificationsRead();
    qc.invalidateQueries({ queryKey: ["notificationsUnread"] });
    qc.invalidateQueries({ queryKey: ["notificationsList"] });
  }

  // Open a notification's source: navigate to its deep-link, mark it read, and close.
  function openNote(n: AppNotification) {
    const to = notificationLink(n);
    if (!n.read) void markRead(n.id);
    setOpen(false);
    if (to) navigate(to);
  }

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        title="Notifications"
        className={`relative rounded-lg p-1.5 text-gray-500 transition hover:bg-gray-200/60 hover:text-gray-700 ${
          collapsed ? "" : ""
        }`}
      >
        <BellIcon className="h-[18px] w-[18px]" />
        {unread > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-semibold text-white">
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 w-80 overflow-hidden rounded-xl border bg-white shadow-2xl">
          <div className="flex items-center justify-between border-b px-3 py-2">
            <span className="text-sm font-semibold text-gray-800">Notifications</span>
            <button onClick={() => void markAll()} className="text-[11px] text-brand hover:underline">
              Mark all read
            </button>
          </div>
          <div className="max-h-96 overflow-y-auto">
            {notes.length === 0 && (
              <div className="px-3 py-8 text-center text-xs text-gray-400">No notifications.</div>
            )}
            {notes.map((n) => {
              const to = notificationLink(n);
              return (
              <div
                key={n.id}
                onClick={() => openNote(n)}
                role={to ? "button" : undefined}
                tabIndex={to ? 0 : undefined}
                onKeyDown={to ? (e) => { if (e.key === "Enter") openNote(n); } : undefined}
                className={`flex gap-2 border-b px-3 py-2 last:border-0 ${n.read ? "opacity-60" : "bg-brand/5"} ${to ? "cursor-pointer hover:bg-brand/10" : ""}`}
              >
                <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${SEV_DOT[n.severity] ?? "bg-gray-400"}`} />
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-medium text-gray-800">{n.title}</div>
                  <div className="line-clamp-2 text-[11px] text-gray-500">{n.body}</div>
                  <div className="mt-0.5 flex items-center gap-2 text-[10px] text-gray-400">
                    <span>{n.created_at ? formatRelativeFromNow(n.created_at) : ""}</span>
                    <span>·</span>
                    <span>{n.source}</span>
                    {to && <span className="text-brand">· Open →</span>}
                    {!n.read && (
                      <button onClick={(e) => { e.stopPropagation(); void markRead(n.id); }} className="ml-auto text-brand hover:underline">
                        Mark read
                      </button>
                    )}
                  </div>
                </div>
              </div>
              );
            })}
          </div>
          <Link
            to="/notifications"
            onClick={() => setOpen(false)}
            className="block border-t px-3 py-2 text-center text-[11px] font-medium text-brand hover:bg-brand/5"
          >
            See all notifications →
          </Link>
        </div>
      )}
    </div>
  );
}

function BellIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
      <path d="M6 9a6 6 0 0112 0c0 5 1.5 6.5 2 7H4c.5-.5 2-2 2-7Z" strokeLinejoin="round" />
      <path d="M9.5 20a2.5 2.5 0 005 0" strokeLinecap="round" />
    </svg>
  );
}
