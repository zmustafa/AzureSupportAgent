import assert from "node:assert/strict";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const budgets = { entryRawKB: 2, entryGzipKB: 0, totalRawKB: 3, largestChunkRawKB: 2 };

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), "bundle-budget-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const scripts = join(root, "scripts");
  const assets = join(root, "dist", "assets");
  mkdirSync(scripts);
  const script = join(scripts, "bundle-budget.mjs");
  const config = join(scripts, "bundle-budget.json");
  copyFileSync(join(HERE, "bundle-budget.mjs"), script);
  return {
    config,
    build() {
      mkdirSync(assets, { recursive: true });
      writeFileSync(join(assets, "index-ABC12345.js"), "x".repeat(2048));
      writeFileSync(join(assets, "vendor-ABC12345.js"), "y".repeat(1024));
    },
    run(...args) {
      const result = spawnSync(process.execPath, [script, ...args], { encoding: "utf8" });
      assert.ifError(result.error);
      return result;
    },
  };
}

test("missing build fails explicitly", (t) => {
  const f = fixture(t);
  const result = f.run();
  assert.equal(result.status, 2);
  assert.match(result.stderr, /No build found/);
});

for (const args of [[], ["--report"]]) {
  test(`missing budget fails explicitly with ${args.join(" ") || "default mode"}`, (t) => {
    const f = fixture(t);
    f.build();
    const result = f.run(...args);
    assert.equal(result.status, 2);
    assert.match(result.stderr, /Run with --update to create it/);
  });
}

test("update creates a missing budget with the measured shape", (t) => {
  const f = fixture(t);
  f.build();
  assert.equal(f.run("--update").status, 0);
  assert.deepEqual(JSON.parse(readFileSync(f.config, "utf8")), {
    budgets: { ...budgets, chunkCount: 2 },
  });
});

test("update preserves opt-in chunks", (t) => {
  const f = fixture(t);
  f.build();
  writeFileSync(f.config, JSON.stringify({ budgets, optInChunks: ["vendor.js"] }));
  assert.equal(f.run("--update").status, 0);
  assert.deepEqual(JSON.parse(readFileSync(f.config, "utf8")), {
    budgets: { ...budgets, chunkCount: 2 },
    optInChunks: ["vendor.js"],
  });
});

test("a valid budget passes and an exceeded budget only passes in report mode", (t) => {
  const f = fixture(t);
  f.build();
  writeFileSync(f.config, JSON.stringify({ budgets }));
  assert.equal(f.run().status, 0);
  writeFileSync(f.config, JSON.stringify({ budgets: { ...budgets, entryRawKB: 1 } }));
  const result = f.run();
  assert.equal(result.status, 1);
  assert.match(result.stderr, /1 budget\(s\) exceeded/);
  assert.equal(f.run("--report").status, 0);
});

test("invalid JSON fails without overwriting the budget", (t) => {
  const f = fixture(t);
  f.build();
  writeFileSync(f.config, "{invalid");
  const result = f.run("--update");
  assert.equal(result.status, 1);
  assert.match(result.stderr, /SyntaxError/);
  assert.equal(readFileSync(f.config, "utf8"), "{invalid");
});

test("a budget read error is not treated as a missing file", (t) => {
  const f = fixture(t);
  f.build();
  mkdirSync(f.config);
  const result = f.run("--update");
  assert.equal(result.status, 1);
  assert.doesNotMatch(result.stdout, /Budget rewritten/);
});
