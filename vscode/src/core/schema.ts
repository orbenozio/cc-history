// CLI §5 schema, mirrored verbatim so a cc-history-vscode index.db is
// interchangeable with the Python CLI's. Uses IF NOT EXISTS so opening an
// existing CLI-created DB is a no-op.
//
// Ground truth: ../../cc-history/cc_history.py — SCHEMA.

export const SCHEMA = `
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    session_id TEXT,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    last_offset INTEGER NOT NULL,
    last_indexed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_session ON files(session_id);
CREATE INDEX IF NOT EXISTS idx_files_project ON files(project);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    project TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    ts TEXT,
    role TEXT,
    kind TEXT NOT NULL,
    tool_name TEXT,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_session ON entries(session_id);
CREATE INDEX IF NOT EXISTS idx_entries_project_ts ON entries(project, ts);
CREATE INDEX IF NOT EXISTS idx_entries_kind ON entries(kind);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    text,
    content='entries',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
  INSERT INTO entries_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, text) VALUES('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
`;

// Statements to wipe the index for a --full rebuild (mirrors the CLI's drop order).
export const DROP_ALL = `
DROP TABLE IF EXISTS entries_fts;
DROP TRIGGER IF EXISTS entries_ai;
DROP TRIGGER IF EXISTS entries_ad;
DROP TABLE IF EXISTS entries;
DROP TABLE IF EXISTS files;
`;
