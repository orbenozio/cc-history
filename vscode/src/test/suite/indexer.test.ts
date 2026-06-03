// Integration test for the incremental indexer against the real
// ~/.claude/projects. Skips cleanly where there's no Claude install (e.g. CI),
// so it's a local exit-criterion check rather than a CI gate.

import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as assert from "assert";
import { BetterSqlite3Engine } from "../../core/sqliteEngine";
import { runIndex, claudeProjectsDir } from "../../core/indexer";
import { buildFtsQuery } from "../../core/query";

function hasRealTranscripts(): boolean {
  const dir = claudeProjectsDir();
  try {
    return fs.readdirSync(dir).length > 0;
  } catch {
    return false;
  }
}

export function testIndexerIntegration(): void {
  if (!hasRealTranscripts()) {
    console.log("[indexer] SKIP — no ~/.claude/projects on this machine");
    return;
  }

  const engine = new BetterSqlite3Engine();
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "cc-history-idx-"));
  const dbPath = path.join(tmpDir, "index.db");
  const db = engine.openReadWrite(dbPath);

  try {
    const first = runIndex(db);
    assert.ok(first.entries > 0, "expected a non-zero entry count from a full index");
    assert.strictEqual(first.errors, 0, "indexing should not hard-error");

    const byKind = Object.fromEntries(
      (
        db.prepare("SELECT kind, COUNT(*) c FROM entries GROUP BY kind").all() as Array<{
          kind: string;
          c: number;
        }>
      ).map((r) => [r.kind, r.c])
    );
    assert.ok(byKind.text > 0, "expected text entries");
    assert.ok(byKind.tool_use > 0, "expected tool_use entries");

    // Incremental: a second pass re-reads nothing.
    const second = runIndex(db);
    assert.strictEqual(second.entries, 0, "second pass should add 0 entries");
    assert.ok(second.skipped > 0, "second pass should skip unchanged files");

    // Timing check (spec Phase 1): FTS MATCH + ORDER BY ts DESC LIMIT 50, warm.
    const sql =
      "SELECT e.id, e.ts, snippet(entries_fts,0,'…','…','…',16) s " +
      "FROM entries_fts f JOIN entries e ON e.id=f.rowid " +
      "WHERE entries_fts MATCH ? ORDER BY e.ts DESC LIMIT 50";
    const stmt = db.prepare(sql);
    const q = buildFtsQuery("the"); // common token to force a large match set
    stmt.all(q); // warm
    const t0 = Date.now();
    const rows = stmt.all(q);
    const ms = Date.now() - t0;
    console.log(
      `[indexer] PASS — ${first.entries} entries (kinds=${JSON.stringify(byKind)}), ` +
        `incremental 0-new, query ${rows.length} rows in ${ms}ms`
    );
    assert.ok(ms < 50, `FTS query took ${ms}ms warm, budget 50ms`);

    // Hebrew search works end-to-end on the real index (best-effort).
    const heb = db.prepare(sql).all(buildFtsQuery("שלום")) as unknown[];
    console.log(`[indexer] Hebrew query returned ${heb.length} rows`);
  } finally {
    db.close();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}
