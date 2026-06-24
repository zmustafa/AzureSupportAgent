import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    react(),
    // Precache built JS/CSS assets so repeat visits load instantly even when
    // the network is slow. We deliberately do NOT cache API responses — keeping
    // dynamic data fresh is more important than offline access for this app.
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["favicon.ico"],
      // Only emit a service worker in production builds (`npm run build`); the
      // dev server stays SW-free so HMR isn't intercepted.
      devOptions: { enabled: false },
      workbox: {
        // Precache the built static assets (matches Vite's hashed output).
        globPatterns: ["**/*.{js,css,html,svg,png,ico,woff2}"],
        // Bypass anything under /api — those are dynamic and must hit the
        // backend, never the SW cache.
        navigateFallbackDenylist: [/^\/api\//],
        maximumFileSizeToCacheInBytes: 3 * 1024 * 1024,
        runtimeCaching: [
          // For static assets requested at runtime (e.g. lazy-loaded chunks),
          // serve from cache first and revalidate in the background.
          {
            urlPattern: /\.(?:js|css|woff2)$/,
            handler: "StaleWhileRevalidate",
            options: { cacheName: "azsup-assets" },
          },
        ],
      },
      manifest: {
        name: "Azure Support Agent",
        short_name: "AzSupAgent",
        theme_color: "#2563eb",
        background_color: "#ffffff",
        display: "standalone",
        start_url: "/dashboard",
        icons: [],
      },
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
