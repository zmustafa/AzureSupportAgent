import { QueryClient } from "@tanstack/react-query";

// Shared singleton QueryClient. Lives in its own module (no component imports) so that
// non-component modules — e.g. the Performance Profiler's background-run registry — can
// invalidate queries without creating a circular import with main.tsx.
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Treat data as fresh for 30s so navigating between views doesn't re-hit the
      // API for config/chats/connections on every mount (the previous default of 0
      // caused a refetch storm). Mutations still invalidate explicitly when needed.
      staleTime: 30_000,
      gcTime: 5 * 60_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});
