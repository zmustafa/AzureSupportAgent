// The deployed image/release version, shown in the top header.
// Baked in at Docker build time via the VITE_APP_VERSION build arg (set to the image tag,
// e.g. "v29"); falls back to "dev" for a local `npm run dev` where no tag is baked.
export const APP_VERSION: string = import.meta.env.VITE_APP_VERSION || "dev";
