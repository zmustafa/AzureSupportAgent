import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    react(),
    // SERVICE WORKER REMOVAL (self-destroying). This app is online-only — API responses are
    // deliberately never cached, the backend serves index.html no-cache + hashed assets
    // immutable, so a service worker provided ~no benefit while causing real staleness bugs:
    // a still-active OLD worker served STALE lazily-loaded route chunks (e.g. v72 Performance
    // code) even after a hard refresh updated the document to v73, and the "waiting" worker
    // re-prompted endlessly. `selfDestroying: true` emits a sw.js that UNREGISTERS the existing
    // worker and DELETES all its caches on the client's next visit, then this plugin is removed
    // entirely in a follow-up release. Reliable freshness now comes from plain HTTP caching +
    // the lightweight /version poll banner in src/pwa.ts (long-open tabs still get notified).
    VitePWA({
      selfDestroying: true,
      injectRegister: null,
      devOptions: { enabled: false },
    }),
  ],
  server: {
    port: 5173,
    host: true,
  },
  build: {
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      output: {
        // Split heavy third-party libraries into their own chunks so they cache
        // across deploys and don't bloat individual route chunks. We deliberately
        // do NOT group mermaid / cytoscape / recharts — those packages already
        // ship dynamic sub-chunks (one per diagram type, etc.) and merging them
        // here would defeat that and produce multi-MB single chunks.
        manualChunks(id: string) {
          if (!id.includes("node_modules")) return undefined;
          // React runtime CORE must all live in ONE chunk — react, react-dom,
          // react/jsx-runtime, react-is, scheduler, use-sync-external-store. If any
          // (esp. `scheduler`, a react-dom dependency) leaks into another vendor chunk,
          // Rollup can emit a circular chunk init where a React-consuming chunk
          // (e.g. vendor-xyflow) evaluates before React's exports are ready → the
          // production-only "Cannot read properties of undefined (reading 'useState')"
          // white-screen crash. Checked FIRST so nothing else can claim these paths.
          if (
            id.includes("/node_modules/react/") ||
            id.includes("/node_modules/react-dom/") ||
            id.includes("/node_modules/react-is/") ||
            id.includes("/node_modules/scheduler/") ||
            id.includes("/node_modules/use-sync-external-store/")
          ) return "vendor-react";
          if (id.includes("/react-router") || id.includes("/@remix-run/")) return "vendor-router";
          if (id.includes("/@tanstack/")) return "vendor-query";
          if (id.includes("/@xyflow/")) return "vendor-xyflow";
          if (id.includes("/katex")) return "vendor-katex";
          if (id.includes("/react-markdown") || id.includes("/remark") || id.includes("/micromark") || id.includes("/mdast") || id.includes("/hast") || id.includes("/unist")) return "vendor-markdown";
          if (id.includes("/jszip")) return "vendor-jszip";
          if (id.includes("/react-grid-layout") || id.includes("/react-draggable") || id.includes("/react-resizable")) return "vendor-grid";
          if (id.includes("/topojson") || id.includes("/world-atlas")) return "vendor-geo";
          return undefined;
        },
      },
    },
  },
});
