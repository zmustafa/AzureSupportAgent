export function MissionControlLoading({ detail = false }: { detail?: boolean }) {
  const label = detail ? "Loading mission board…" : "Loading Mission Control…";
  return (
    <div
      data-testid="mission-control-loading"
      role="status"
      aria-live="polite"
      aria-busy="true"
      aria-label={label}
      className="flex h-full min-h-52 items-center justify-center bg-gray-50 p-6"
    >
      <div className="w-full max-w-sm rounded-2xl border border-blue-100 bg-white/90 px-6 py-5 text-center shadow-sm backdrop-blur">
        <div aria-hidden="true" className="relative mx-auto h-16 w-48 overflow-hidden">
          <div className="absolute inset-x-3 top-8 h-px bg-gradient-to-r from-transparent via-blue-200 to-transparent" />
          <div className="absolute left-3 top-2 animate-hourglass-slide motion-reduce:left-1/2 motion-reduce:-translate-x-1/2 motion-reduce:animate-none">
            <div className="animate-hourglass-hover motion-reduce:animate-none">
              <svg
                data-testid="mission-control-hourglass"
                viewBox="0 0 32 40"
                className="h-11 w-9 text-brand drop-shadow-sm"
              >
                <path d="M7 3h18M7 37h18" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
                <path d="M9 5c0 8 2.2 10.2 7 14-4.8 3.8-7 6-7 14h14c0-8-2.2-10.2-7-14 4.8-3.8 7-6 7-14H9Z" fill="white" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
                <path d="M12 8h8c-.4 4.2-1.8 6.2-4 8-2.2-1.8-3.6-3.8-4-8Zm0 22c.5-3.5 1.9-5.3 4-7 2.1 1.7 3.5 3.5 4 7h-8Z" fill="currentColor" opacity=".55" />
                <circle cx="16" cy="20" r="1.2" fill="currentColor" />
              </svg>
            </div>
          </div>
        </div>
        <div className="mt-1 text-sm font-semibold text-gray-700">{label}</div>
        <p className="mt-1 text-xs text-gray-500">Preparing cached workload systems and mission history.</p>
      </div>
    </div>
  );
}
