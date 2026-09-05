import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { minify } from "terser";

export default defineConfig({
  plugins: [
    react(),
    {
      name: "safe-module-compression",
      apply: "build",
      enforce: "post",
      // Compress within the bundler's hash pipeline, never rewrite hashed files
      // afterwards. Keep property names, license comments and unsafe transforms
      // unchanged. Oxc still performs the final output minification pass.
      async renderChunk(code) {
        const result = await minify(code, {
          ecma: 2020, module: true, compress: { passes: 3 },
          format: { comments: "some", ascii_only: false },
        });
        if (result.code === undefined) throw new Error("Module compression produced no code");
        return { code: result.code, map: null };
      },
    },
    // NO SERVICE WORKER. This app is online-only: API responses are deliberately never
    // cached, and the backend serves index.html no-cache + hashed assets immutable, so a
    // service worker provided ~no benefit while causing real staleness bugs (a still-active
    // OLD worker served STALE lazily-loaded route chunks even after a hard refresh, and the
    // "waiting" worker re-prompted endlessly).
    //
    // History: `vite-plugin-pwa` was kept temporarily with `selfDestroying: true` to emit an
    // sw.js that unregistered existing workers. That shim has now served its purpose across
    // many releases and the plugin is REMOVED (planned in its own comment; done 2026-07-31,
    // which also drops 8 high-severity transitive advisories via workbox-build -> ejs/jake/
    // filelist/minimatch/brace-expansion, none of which had an upstream fix).
    //
    // Cleanup is now done by the app itself: `_killServiceWorkers()` in src/pwa.ts unregisters
    // ANY leftover worker and deletes all caches on every load, so this does not depend on
    // sw.js still being served. Freshness comes from plain HTTP caching + the /version poll
    // banner in src/pwa.ts.
  ],
  server: {
    // Pinned local dev ports: frontend 35000, backend 35001. strictPort matters here —
    // without it a busy 35000 slides Vite onto 35001, which is the backend.
    port: 35000,
    strictPort: true,
    host: true,
  },
  build: {
    minify: "oxc",
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
