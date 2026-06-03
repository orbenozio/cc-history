// Phase 0 spike — the actual proof.
//
// Runs inside a worker_thread. Loads the NATIVE better-sqlite3 module, builds
// the CLI §5 FTS5 schema (unicode61 remove_diacritics 2 tokenizer), inserts one
// English and one Hebrew row, and runs a MATCH for a Hebrew word. Posts the
// result (plus ABI diagnostics) back to the host.
//
// The whole point: this must succeed when loaded from a packaged-and-installed
// .vsix, inside VS Code's Electron, on macOS and Windows.

import { parentPort } from "worker_threads";
import * as os from "os";
import * as path from "path";
import * as fs from "fs";

export interface FtsProbeResult {
  ok: boolean;
  hebrewMatchHits: number;
  matchedText: string | null;
  diacriticsFold: boolean;
  betterSqlite3Version: string | null;
  sqliteVersion: string | null;
  abiModules: string;
  electron: string | null;
  node: string;
  error: string | null;
}

const SCHEMA = `
CREATE TABLE entries (
  id INTEGER PRIMARY KEY,
  role TEXT,
  kind TEXT NOT NULL,
  text TEXT NOT NULL
);
CREATE VIRTUAL TABLE entries_fts USING fts5(
  text,
  content='entries',
  content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER entries_ai AFTER INSERT ON entries BEGIN
  INSERT INTO entries_fts(rowid, text) VALUES (new.id, new.text);
END;
`;

export function runProbe(): FtsProbeResult {
  const result: FtsProbeResult = {
    ok: false,
    hebrewMatchHits: 0,
    matchedText: null,
    diacriticsFold: false,
    betterSqlite3Version: null,
    sqliteVersion: null,
    abiModules: process.versions.modules,
    electron: (process.versions as Record<string, string>).electron ?? null,
    node: process.versions.node,
    error: null,
  };

  let dbPath: string | null = null;
  try {
    // Load the native module — the thing under test.
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const Database = require("better-sqlite3");

    dbPath = path.join(
      fs.mkdtempSync(path.join(os.tmpdir(), "cc-history-spike-")),
      "probe.db"
    );
    const db = new Database(dbPath);
    db.pragma("journal_mode = WAL");
    result.sqliteVersion = db
      .prepare("SELECT sqlite_version() AS v")
      .get().v;

    db.exec(SCHEMA);
    const insert = db.prepare(
      "INSERT INTO entries (role, kind, text) VALUES (?,?,?)"
    );
    insert.run("user", "text", "the quick brown fox jumps");
    insert.run("assistant", "text", "שלום עולם, איך הולך היום?");
    // A diacritics-folding check: niqqud should be stripped by remove_diacritics 2.
    insert.run("user", "text", "שָׁלוֹם עִם נִיקּוּד");

    const rows = db
      .prepare(
        `SELECT e.text AS text
           FROM entries_fts f
           JOIN entries e ON e.id = f.rowid
          WHERE entries_fts MATCH ?
          ORDER BY rank`
      )
      .all("שלום") as Array<{ text: string }>;

    result.hebrewMatchHits = rows.length;
    result.matchedText = rows[0]?.text ?? null;
    // If MATCH 'שלום' (no niqqud) also catches the niqqud row, folding works.
    result.diacriticsFold = rows.some((r) => r.text.includes("נִיקּוּד"));
    result.betterSqlite3Version =
      (Database as { prototype?: unknown }) &&
      // better-sqlite3 exposes VERSION on the constructor in recent versions
      ((Database as unknown as { VERSION?: string }).VERSION ?? null);

    db.close();
    result.ok = result.hebrewMatchHits >= 1 && result.matchedText !== null;
  } catch (err) {
    result.error = err instanceof Error ? `${err.message}\n${err.stack}` : String(err);
  } finally {
    if (dbPath) {
      try {
        fs.rmSync(path.dirname(dbPath), { recursive: true, force: true });
      } catch {
        /* best effort */
      }
    }
  }
  return result;
}

// When spawned as a worker, run immediately and post the result.
if (parentPort) {
  parentPort.postMessage(runProbe());
}
