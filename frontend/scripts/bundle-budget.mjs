#!/usr/bin/env node
/**
 * Bundle budget gate.
 *
 * The entry chunk is what every user downloads before first paint, so it is the number the
 * API-client split (plan 07) is trying to move. Without a gate the improvement is invisible
 * and the regression is silent.
 *
 *   node scripts/bundle-budget.mjs            # report + enforce
 *   node scripts/bundle-budget.mjs --report   # report only, never fails
 *   node scripts/bundle-budget.mjs --update   # rewrite the budget to today's sizes
 *
 * Budgets live in bundle-budget.json next to this script.
 */
import { gzipSync } from "node:zlib";
import { readFileSync, readdirSync, statSync, writeFileSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const DIST = resolve(HERE, "..", "dist", "assets");
const BUDGET_FILE = join(HERE, "bundle-budget.json");
const KB = 1024;

const args = new Set(process.argv.slice(2));
const reportOnly = args.has("--report");
const update = args.has("--update");

if (!existsSync(DIST)) {
  console.error(`No build found at ${DIST}. Run \`npm run build\` first.`);
  process.exit(2);
}

const js = readdirSync(DIST).filter((f) => f.endsWith(".js"));
if (js.length === 0) {
  console.error(`No .js emitted into ${DIST}.`);
  process.exit(2);
}

/** Vite names the entry `index-<hash>.js`; everything else is a route or vendor chunk. */
const isEntry = (name) => /^index-[A-Za-z0-9_-]+\.js$/.test(name);

const chunks = js.map((name) => {
  const buf = readFileSync(join(DIST, name));
  return {
    name,
    // Strip the content hash so a budget survives a rebuild.
    key: name.replace(/-[A-Za-z0-9_-]{6,}\.js$/, ".js"),
    raw: statSync(join(DIST, name)).size,
    gzip: gzipSync(buf).length,
    entry: isEntry(name),
  };
});

const entry = chunks.find((c) => c.entry);
const totalRaw = chunks.reduce((n, c) => n + c.raw, 0);
const totalGzip = chunks.reduce((n, c) => n + c.gzip, 0);

const measured = {
  entryRawKB: entry ? Math.round(entry.raw / KB) : 0,
  entryGzipKB: entry ? Math.round(entry.gzip / KB) : 0,
  totalRawKB: Math.round(totalRaw / KB),
  chunkCount: chunks.length,
  largestChunkRawKB: Math.round(Math.max(...chunks.map((c) => c.raw)) / KB),
};

if (update) {
  writeFileSync(BUDGET_FILE, `${JSON.stringify({ budgets: measured }, null, 2)}\n`);
  console.log(`Budget rewritten to today's sizes:\n${JSON.stringify(measured, null, 2)}`);
  process.exit(0);
}

if (!existsSync(BUDGET_FILE)) {
  console.error(`No ${BUDGET_FILE}. Run with --update to create it.`);
  process.exit(2);
}
const { budgets } = JSON.parse(readFileSync(BUDGET_FILE, "utf8"));

console.log(`\n  ${chunks.length} chunks, ${measured.totalRawKB} KB raw / ${Math.round(totalGzip / KB)} KB gzip\n`);
console.log("  Largest chunks");
for (const c of [...chunks].sort((a, b) => b.raw - a.raw).slice(0, 8)) {
  const tag = c.entry ? "  <-- ENTRY (first paint)" : "";
  console.log(`    ${String(Math.round(c.raw / KB)).padStart(5)} KB  ${String(Math.round(c.gzip / KB)).padStart(4)} KB gz  ${c.key}${tag}`);
}

const checks = [
  ["entry chunk raw", measured.entryRawKB, budgets.entryRawKB, "KB"],
  ["entry chunk gzip", measured.entryGzipKB, budgets.entryGzipKB, "KB"],
  ["total JS raw", measured.totalRawKB, budgets.totalRawKB, "KB"],
  ["largest single chunk", measured.largestChunkRawKB, budgets.largestChunkRawKB, "KB"],
];

console.log("\n  Budgets");
let failed = 0;
for (const [label, actual, budget, unit] of checks) {
  const over = actual > budget;
  if (over) failed++;
  const delta = actual - budget;
  const sign = delta > 0 ? `+${delta}` : `${delta}`;
  console.log(`    ${over ? "OVER" : "ok  "}  ${label.padEnd(22)} ${String(actual).padStart(5)} ${unit}  (budget ${budget} ${unit}, ${sign})`);
}

if (!entry) {
  console.error("\n  Could not identify the entry chunk (no index-*.js).");
  process.exit(2);
}

if (failed && !reportOnly) {
  console.error(`\n  ${failed} budget(s) exceeded. Re-run with --update only if the growth is intended.\n`);
  process.exit(1);
}
console.log(failed ? "\n  (report-only; not failing)\n" : "\n  All budgets met.\n");
