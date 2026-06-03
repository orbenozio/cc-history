// Incremental indexer — TS port of cc_history.py index_file + cmd_index.
// Walks ~/.claude/projects/**/*.jsonl, resumes per file from a byte offset, and
// writes each file's rows + its `files` bookkeeping row in ONE BEGIN IMMEDIATE
// transaction (a crash mid-file rolls back and leaves last_offset untouched).
//
// Ground truth: ../../cc-history/cc_history.py.

import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { SqliteDb } from "./sqliteEngine";
import { SCHEMA, DROP_ALL } from "./schema";
import { parseLine } from "./parser";
import { resolveProject } from "./decode";

const MAX_MALFORMED_PER_FILE = 10;

export interface IndexSummary {
  entries: number;
  files: number;
  skipped: number;
  errors: number;
  elapsedMs: number;
}

export interface IndexOptions {
  projectsDir?: string;
  full?: boolean;
  onProgress?: (msg: string) => void;
  isCancelled?: () => boolean;
}

export function claudeProjectsDir(): string {
  return path.join(os.homedir(), ".claude", "projects");
}

function nowIsoZ(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function mtimeIsoZ(mtimeMs: number): string {
  return new Date(mtimeMs).toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** Scan the first few lines of a transcript for a `cwd` field. */
function readCwdHint(filePath: string): string | null {
  try {
    const head = fs.readFileSync(filePath, "utf8").split(/\n/, 8);
    for (const line of head) {
      const t = line.trim();
      if (!t) {
        continue;
      }
      try {
        const obj = JSON.parse(t);
        if (typeof obj.cwd === "string" && obj.cwd) {
          return obj.cwd;
        }
      } catch {
        /* ignore */
      }
    }
  } catch {
    /* ignore */
  }
  return null;
}

class FileAbandoned extends Error {}

/**
 * Index one file from `startOffset` inside a single immediate transaction.
 * Returns entries inserted (0 if the file was abandoned and rolled back).
 */
export function indexFile(
  db: SqliteDb,
  filePath: string,
  project: string,
  startOffset: number
): number {
  const st = fs.statSync(filePath, { bigint: true });
  const fileMtimeIso = mtimeIsoZ(Number(st.mtimeMs));
  const buf = fs.readFileSync(filePath);
  const slice = buf.subarray(startOffset);
  const newOffset = startOffset + slice.length;
  const text = slice.toString("utf8");
  const fileStem = path.basename(filePath).replace(/\.jsonl$/i, "");

  let inserted = 0;
  let malformed = 0;
  let sessionIdForFile: string | null = null;

  const insert = db.prepare(
    "INSERT INTO entries (session_id, project, file_path, line_no, ts, role, kind, tool_name, text) " +
      "VALUES (?,?,?,?,?,?,?,?,?)"
  );

  try {
    db.immediateTransaction(() => {
      const lines = text.split(/\n/);
      let lineNo = 0;
      for (const rawLine of lines) {
        lineNo++;
        const line = rawLine.trim();
        if (!line) {
          continue;
        }
        let obj: Record<string, unknown>;
        try {
          obj = JSON.parse(line);
        } catch {
          malformed++;
          if (malformed >= MAX_MALFORMED_PER_FILE) {
            throw new FileAbandoned();
          }
          continue;
        }
        const { ts, entries } = parseLine(obj);
        if (entries.length === 0) {
          continue;
        }
        const sessionId = (obj.sessionId as string) || fileStem;
        if (sessionIdForFile === null) {
          sessionIdForFile = sessionId;
        }
        const tsVal = ts ?? fileMtimeIso;
        for (const e of entries) {
          insert.run(
            sessionId,
            project,
            filePath,
            lineNo,
            tsVal,
            e.role,
            e.kind,
            e.tool_name,
            e.text
          );
          inserted++;
        }
      }
      db.prepare(
        "INSERT INTO files (path, project, session_id, mtime_ns, size, last_offset, last_indexed_at) " +
          "VALUES (?,?,?,?,?,?,?) " +
          "ON CONFLICT(path) DO UPDATE SET project=excluded.project, " +
          "session_id=excluded.session_id, mtime_ns=excluded.mtime_ns, " +
          "size=excluded.size, last_offset=excluded.last_offset, " +
          "last_indexed_at=excluded.last_indexed_at"
      ).run(
        filePath,
        project,
        sessionIdForFile,
        st.mtimeNs, // BigInt — stored as 64-bit INTEGER, precision-safe
        st.size,
        newOffset,
        nowIsoZ()
      );
    });
  } catch (err) {
    if (err instanceof FileAbandoned) {
      return 0; // rolled back; last_offset unchanged so the next run retries
    }
    throw err;
  }
  return inserted;
}

function listJsonlFiles(projectsDir: string): string[] {
  const out: string[] = [];
  let dirs: fs.Dirent[];
  try {
    dirs = fs.readdirSync(projectsDir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const d of dirs) {
    if (!d.isDirectory()) {
      continue;
    }
    const sub = path.join(projectsDir, d.name);
    let files: string[];
    try {
      files = fs.readdirSync(sub);
    } catch {
      continue;
    }
    for (const f of files) {
      if (f.toLowerCase().endsWith(".jsonl")) {
        out.push(path.join(sub, f));
      }
    }
  }
  return out.sort();
}

/** Full incremental pass. Initializes/clears schema as needed and updates meta.last_run. */
export function runIndex(db: SqliteDb, opts: IndexOptions = {}): IndexSummary {
  const projectsDir = opts.projectsDir ?? claudeProjectsDir();
  const started = Date.now();

  db.exec(SCHEMA);
  if (opts.full) {
    db.exec(DROP_ALL);
    db.exec(SCHEMA);
  }

  let entries = 0;
  let filesIndexed = 0;
  let skipped = 0;
  let errors = 0;

  for (const filePath of listJsonlFiles(projectsDir)) {
    if (opts.isCancelled?.()) {
      break;
    }
    let st: fs.BigIntStats;
    try {
      st = fs.statSync(filePath, { bigint: true });
    } catch {
      continue;
    }

    // Unchanged? Compare in SQLite (64-bit) to avoid JS precision loss on ns.
    const unchanged = db
      .prepare("SELECT 1 FROM files WHERE path=? AND mtime_ns=? AND size=?")
      .get(filePath, st.mtimeNs, st.size);
    if (unchanged) {
      skipped++;
      continue;
    }

    const existing = db
      .prepare("SELECT last_offset, project FROM files WHERE path=?")
      .get(filePath) as { last_offset: number; project: string } | undefined;

    const project =
      existing?.project ?? resolveProject(path.basename(path.dirname(filePath)), readCwdHint(filePath));

    let startOffset = existing?.last_offset ?? 0;
    if (startOffset > Number(st.size)) {
      startOffset = 0; // file shrank/rotated — re-read from scratch
    }

    try {
      entries += indexFile(db, filePath, project, startOffset);
      filesIndexed++;
      opts.onProgress?.(`indexed ${path.basename(filePath)}`);
    } catch (err) {
      errors++;
      opts.onProgress?.(`error ${path.basename(filePath)}: ${String(err)}`);
    }
  }

  const elapsedMs = Date.now() - started;
  // meta.last_run JSON shape must match the CLI exactly (no `errors` key).
  db.prepare(
    "INSERT INTO meta (key, value) VALUES ('last_run', ?) " +
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
  ).run(
    JSON.stringify({
      at: nowIsoZ(),
      entries,
      files: filesIndexed,
      skipped,
      elapsed: Math.round((elapsedMs / 1000) * 1000) / 1000,
    })
  );
  db.pragma("wal_checkpoint(PASSIVE)");

  return { entries, files: filesIndexed, skipped, errors, elapsedMs };
}
