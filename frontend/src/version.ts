// The deployed image/release version, shown in the top header.
// Baked in at Docker build time via the VITE_APP_VERSION build arg. The version is PINNED to
// "v1" (no auto-increment), but the build arg carries a unique build id "v1+<gitsha>" so the
// stale-bundle Reload banner can still tell two builds apart (it compares the FULL string).
// Falls back to "dev" for a local `npm run dev` where no tag is baked.
export const APP_VERSION: string = import.meta.env.VITE_APP_VERSION || "dev";
// Human-friendly label for the header pill / About dialog — drops the "+<gitsha>" build suffix so
// the user just sees "v1" (e.g. "v1+a1b2c3d" -> "v1").
export const APP_VERSION_DISPLAY: string = APP_VERSION.split("+")[0];
