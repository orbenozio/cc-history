// Runs inside VS Code's extension host (real Electron). Phase 1 adds the
// golden-file conformance + unit suites; the Phase 0 worker FTS probe stays as
// a regression that the native engine still loads and queries Hebrew.

import * as path from "path";
import * as assert from "assert";
import { runProbeInWorker } from "../../extension";
import { testConformance } from "./conformance.test";
import { testDecode, testQuery, testLock } from "./unit.test";
import { testIndexerIntegration } from "./indexer.test";

export async function run(): Promise<void> {
  // 1. Parser/truncation byte-exact parity with the CLI.
  testConformance();
  // 2. decode + query + cross-process lock helpers.
  testDecode();
  testQuery();
  testLock();
  // 3. Incremental indexer over the real ~/.claude (skips on CI).
  testIndexerIntegration();

  // 3. Phase 0 regression: native better-sqlite3 FTS5 + Hebrew MATCH in a worker.
  const workerPath = path.resolve(__dirname, "../../ftsWorker.js");
  const r = await runProbeInWorker(workerPath);
  assert.ok(r.ok, `FTS worker probe failed: ${r.error ?? "unknown"}`);
  assert.ok(r.hebrewMatchHits >= 1, "expected a Hebrew MATCH hit");
  assert.strictEqual(r.abiModules, "140", `unexpected ABI ${r.abiModules}`);
  console.log(
    `[engine] PASS — Hebrew FTS5 MATCH in worker (electron=${r.electron}, sqlite=${r.sqliteVersion})`
  );

  console.log("[phase1] ALL PASS");
}
