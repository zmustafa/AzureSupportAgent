/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

declare module "cytoscape-fcose" {
  const ext: (cy: unknown) => void;
  export default ext;
}

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  // App/image version, baked in at Docker build time (e.g. "v29"); falls back to "dev" locally.
  readonly VITE_APP_VERSION?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
