// Lightweight web-vitals reporter. Logs LCP / INP / CLS / TTFB / FCP to the
// browser console on the next idle so we can quickly compare deltas across
// optimization passes. We deliberately do NOT POST these to the backend by
// default — that requires an auth-aware endpoint and is overkill for now. To
// ship them somewhere external, wrap `onMetric` and call `navigator.sendBeacon`
// to a collector.
import { onCLS, onFCP, onINP, onLCP, onTTFB, type Metric } from "web-vitals";

function format(m: Metric): string {
  const v = m.name === "CLS" ? m.value.toFixed(3) : `${Math.round(m.value)}ms`;
  return `[web-vitals] ${m.name}=${v} (${m.rating})`;
}

function onMetric(m: Metric) {
  // Avoid console noise during HMR by gating on production OR a flag in localStorage.
  const enabled =
    import.meta.env.PROD || (typeof window !== "undefined" && window.localStorage.getItem("azsup.vitals") === "1");
  if (!enabled) return;
  // eslint-disable-next-line no-console
  console.info(format(m));
}

export function initWebVitals(): void {
  try {
    onCLS(onMetric);
    onFCP(onMetric);
    onINP(onMetric);
    onLCP(onMetric);
    onTTFB(onMetric);
  } catch {
    // Best-effort: swallow if web-vitals can't initialize in this browser.
  }
}
