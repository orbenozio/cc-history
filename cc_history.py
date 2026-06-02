#!/usr/bin/env python3
"""cc-history — local full-text search over Claude Code session transcripts.

Single-file, stdlib-only. Indexes ~/.claude/projects/**/*.jsonl into a
SQLite+FTS5 database and exposes a CLI for searching. A platform-native
background scheduler (macOS LaunchAgent / Windows Task Scheduler) keeps the
index fresh. See cc-history-spec.md for the full specification.
"""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOOL_USE_MAX = 4096
TOOL_RESULT_MAX = 8192
DEFAULT_INTERVAL = 600
MIN_INTERVAL = 60
DEFAULT_LIMIT = 20
SNIPPET_WIDTH = 120
LOG_ROTATE_BYTES = 5 * 1024 * 1024
MAX_MALFORMED_PER_FILE = 10

# FTS5 operator characters / tokens that signal "pass the query through verbatim".
_FTS_OPERATOR_RE = re.compile(r'["*():]|(?:\b(?:AND|OR|NOT|NEAR)\b)')


# ---------------------------------------------------------------------------
# Paths — single source of truth for filesystem locations (platform-aware)
# ---------------------------------------------------------------------------

class Paths:
    @staticmethod
    def claude_projects_dir() -> Path:
        return Path.home() / ".claude" / "projects"

    @staticmethod
    def app_data_dir() -> Path:
        if sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA")
            if local:
                return Path(local) / "cc-history"
        return Path.home() / ".cc-history"

    @staticmethod
    def db_path() -> Path:
        return Paths.app_data_dir() / "index.db"

    @staticmethod
    def log_path() -> Path:
        return Paths.app_data_dir() / "indexer.log"


# ---------------------------------------------------------------------------
# Logging — structured single-line log written by the script itself
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rotate_log_if_needed(log_path: Path) -> None:
    try:
        if log_path.exists() and log_path.stat().st_size > LOG_ROTATE_BYTES:
            backup = log_path.with_suffix(log_path.suffix + ".1")
            if backup.exists():
                backup.unlink()
            log_path.rename(backup)
    except OSError:
        pass  # never let log rotation crash a run


def log_line(message: str) -> None:
    log_path = Paths.log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{_utc_now_iso()} {message}\n")
    except OSError:
        pass  # logging must never be fatal


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
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
"""


def _check_fts5(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x);")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe;")
    except sqlite3.OperationalError as exc:
        raise SystemExit(
            "This Python's sqlite3 was built without FTS5 support, which "
            "cc-history requires. Install a Python whose sqlite3 includes "
            f"FTS5 and retry. (underlying error: {exc})"
        )


def connect_rw(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def connect_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(
            f"No index found at {db_path}. Run 'cc-history index' (or "
            "'cc-history install') first."
        )
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = connect_rw(db_path)
    _check_fts5(conn)
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Project-name decoding (cross-platform)
# ---------------------------------------------------------------------------

def _path_exists_ci(p: Path) -> bool:
    """Existence check, case-insensitive on Windows."""
    if p.exists():
        return True
    if sys.platform != "win32":
        return False
    parent = p.parent
    if not parent.exists():
        return False
    target = p.name.lower()
    try:
        return any(child.name.lower() == target for child in parent.iterdir())
    except OSError:
        return False


def _decode_greedy(folder_name: str) -> str:
    """Reconstruct a real path from the dash-encoded folder name by walking
    the live filesystem. Used only when no usable cwd is found."""
    parts = folder_name.split("-")

    if sys.platform == "win32":
        # name starts with "<letter>" then an empty part from collapsed ":\"
        non_empty = [p for p in parts if p]
        if not non_empty:
            return folder_name
        drive = non_empty[0]
        current = Path(f"{drive}:\\")
        remaining = non_empty[1:]
        sep_candidates = ("\\",)
    else:
        # name starts with "-" (encoded leading "/"); first element is empty
        non_empty = [p for p in parts if p]
        if not non_empty:
            return folder_name
        current = Path("/" + non_empty[0])
        if not _path_exists_ci(current):
            return folder_name
        remaining = non_empty[1:]
        sep_candidates = ("/",)

    for part in remaining:
        advanced = False
        # priority: separator-join, then ".part", then "_part"
        candidates = [current / part]
        candidates.append(current.parent / f"{current.name}.{part}")
        candidates.append(current.parent / f"{current.name}_{part}")
        for cand in candidates:
            if _path_exists_ci(cand):
                current = cand
                advanced = True
                break
        if not advanced:
            # default to separator join even if it doesn't resolve; keep walking
            current = current / part
    return str(current)


def _decode_naive(folder_name: str) -> str:
    if sys.platform == "win32":
        m = re.match(r"^([A-Za-z])--(.*)$", folder_name)
        if m:
            rest = m.group(2).replace("-", "\\")
            return f"{m.group(1)}:\\{rest}"
        return folder_name.replace("-", "\\")
    return folder_name.replace("-", "/")


def resolve_project(folder: Path, cwd_hint: str | None) -> str:
    """Resolve a dash-encoded project folder to a human-readable path."""
    # 1. cwd fast path (strongly preferred)
    if cwd_hint:
        try:
            if Path(cwd_hint).is_dir():
                return cwd_hint
        except OSError:
            pass
    # 2. greedy resolution against the filesystem
    greedy = _decode_greedy(folder.name)
    if _path_exists_ci(Path(greedy)):
        return greedy
    # 3. naive fallback
    return _decode_naive(folder.name)


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

class ParsedEntry:
    __slots__ = ("role", "kind", "tool_name", "text")

    def __init__(self, role, kind, tool_name, text):
        self.role = role
        self.kind = kind
        self.tool_name = tool_name
        self.text = text


def _stringify(content) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


def _truncate(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    clipped = raw[:limit].decode("utf-8", errors="ignore")
    return f"{clipped}\n[…truncated, full length {len(raw)} bytes]"


def parse_blocks(role: str, content) -> list[ParsedEntry]:
    """Turn a message.content array into ParsedEntry rows."""
    out: list[ParsedEntry] = []
    if isinstance(content, str):
        if content.strip():
            out.append(ParsedEntry(role, "text", None, content))
        return out
    if not isinstance(content, list):
        return out

    for block in content:
        if not isinstance(block, dict):
            if isinstance(block, str) and block.strip():
                out.append(ParsedEntry(role, "text", None, block))
            continue
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text") or ""
            if txt.strip():
                out.append(ParsedEntry(role, "text", None, txt))
        elif btype == "thinking":
            txt = block.get("thinking") or ""
            if txt.strip():
                out.append(ParsedEntry("assistant", "thinking", None, txt))
        elif btype == "tool_use":
            name = block.get("name") or ""
            payload = _stringify(block.get("input"))
            text = _truncate(f"{name}({payload})", TOOL_USE_MAX)
            out.append(ParsedEntry("assistant", "tool_use", name, text))
        elif btype == "tool_result":
            text = _truncate(_stringify(block.get("content")), TOOL_RESULT_MAX)
            if text.strip():
                out.append(ParsedEntry("user", "tool_result", None, text))
        # image / anything else: skip
    return out


def parse_line(obj: dict) -> tuple[str | None, list[ParsedEntry]]:
    """Parse one JSONL object. Returns (timestamp_or_none, entries)."""
    otype = obj.get("type")
    if otype not in ("user", "assistant"):
        return None, []  # queue-operation, summary, etc.

    role = otype
    ts = obj.get("timestamp")
    message = obj.get("message")

    if isinstance(message, str):
        try:
            message = json.loads(message)
        except (ValueError, TypeError):
            return ts, [ParsedEntry(role, "text", None, message)]

    if isinstance(message, dict):
        content = message.get("content")
        return ts, parse_blocks(role, content)

    return ts, []


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------

def _read_cwd_hint(path: Path) -> str | None:
    """Scan the first few lines of a transcript for a cwd field."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(8):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                cwd = obj.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _session_id_for(path: Path, obj_session: str | None) -> str:
    if obj_session:
        return obj_session
    return path.stem  # filename UUID stem


def index_file(conn: sqlite3.Connection, path: Path, project: str,
               start_offset: int, verbose: bool) -> int:
    """Index one file from start_offset within a single transaction.

    Returns the number of entries inserted. Raises on hard failure (caller
    rolls back)."""
    inserted = 0
    malformed = 0
    file_mtime_iso = datetime.fromtimestamp(
        path.stat().st_mtime, timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    mtime_fallback_logged = False

    conn.execute("BEGIN IMMEDIATE;")
    try:
        with path.open("rb") as fh:
            fh.seek(start_offset)
            line_no = 0
            session_id_for_file: str | None = None
            for raw in fh:
                line_no += 1
                text_line = raw.decode("utf-8", errors="replace").strip()
                if not text_line:
                    continue
                try:
                    obj = json.loads(text_line)
                except ValueError:
                    malformed += 1
                    log_line(f"malformed-line file={path} line~={line_no} (count={malformed})")
                    if malformed >= MAX_MALFORMED_PER_FILE:
                        log_line(f"GIVE-UP file={path} too many malformed lines; rolling back this run")
                        raise _FileAbandoned()
                    continue

                ts, entries = parse_line(obj)
                if not entries:
                    continue

                session_id = _session_id_for(path, obj.get("sessionId"))
                if session_id_for_file is None:
                    session_id_for_file = session_id
                if ts is None:
                    ts = file_mtime_iso
                    if not mtime_fallback_logged:
                        log_line(f"missing-timestamp file={path} using mtime fallback")
                        mtime_fallback_logged = True

                for e in entries:
                    conn.execute(
                        "INSERT INTO entries (session_id, project, file_path, "
                        "line_no, ts, role, kind, tool_name, text) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (session_id, project, str(path), line_no, ts,
                         e.role, e.kind, e.tool_name, e.text),
                    )
                    inserted += 1

            new_offset = fh.tell()

        st = path.stat()
        conn.execute(
            "INSERT INTO files (path, project, session_id, mtime_ns, size, "
            "last_offset, last_indexed_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET project=excluded.project, "
            "session_id=excluded.session_id, mtime_ns=excluded.mtime_ns, "
            "size=excluded.size, last_offset=excluded.last_offset, "
            "last_indexed_at=excluded.last_indexed_at",
            (str(path), project, session_id_for_file, st.st_mtime_ns, st.st_size,
             new_offset, _utc_now_iso()),
        )
        conn.execute("COMMIT;")
    except _FileAbandoned:
        conn.execute("ROLLBACK;")
        return 0
    except Exception:
        conn.execute("ROLLBACK;")
        raise

    if verbose:
        print(f"  {path.name}: +{inserted} entries (offset {start_offset}->{new_offset})")
    return inserted


class _FileAbandoned(Exception):
    """Raised internally to abandon a file with too many malformed lines."""


def cmd_index(args) -> int:
    projects_dir = Paths.claude_projects_dir()
    if not projects_dir.is_dir():
        print(f"Claude projects dir not found: {projects_dir}", file=sys.stderr)
        log_line(f"run error: projects dir missing {projects_dir}")
        return 1

    db_path = Paths.db_path()
    _rotate_log_if_needed(db_path.parent / "indexer.log" if db_path else Paths.log_path())
    log_line(f"run start interval=n/a mode={'full' if args.full else 'incremental'}")

    conn = init_db(db_path)

    if args.full:
        conn.executescript(
            "DROP TABLE IF EXISTS entries_fts;"
            "DROP TRIGGER IF EXISTS entries_ai;"
            "DROP TRIGGER IF EXISTS entries_ad;"
            "DROP TABLE IF EXISTS entries;"
            "DROP TABLE IF EXISTS files;"
        )
        conn.executescript(SCHEMA)

    started = time.monotonic()
    total_entries = 0
    files_indexed = 0
    files_skipped = 0
    errors = 0

    jsonl_files = sorted(projects_dir.glob("*/*.jsonl"))
    for path in jsonl_files:
        try:
            st = path.stat()
        except OSError:
            continue

        row = conn.execute(
            "SELECT mtime_ns, size, last_offset, project FROM files WHERE path = ?",
            (str(path),),
        ).fetchone()

        if row and row["mtime_ns"] == st.st_mtime_ns and row["size"] == st.st_size:
            files_skipped += 1
            continue

        if row and row["project"]:
            project = row["project"]
        else:
            cwd_hint = _read_cwd_hint(path)
            project = resolve_project(path.parent, cwd_hint)

        start_offset = row["last_offset"] if row else 0
        # If the file shrank (truncated/rotated), re-read from scratch.
        if start_offset > st.st_size:
            start_offset = 0

        try:
            n = index_file(conn, path, project, start_offset, args.verbose)
            total_entries += n
            files_indexed += 1
        except Exception as exc:  # noqa: BLE001 — keep going across files
            errors += 1
            log_line(f"file error file={path}: {exc!r}")
            if args.verbose:
                print(f"  ERROR {path.name}: {exc}", file=sys.stderr)

    elapsed = time.monotonic() - started
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('last_run', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps({
            "at": _utc_now_iso(),
            "entries": total_entries,
            "files": files_indexed,
            "skipped": files_skipped,
            "elapsed": round(elapsed, 3),
        }),),
    )
    conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
    conn.close()

    summary = (f"indexed entries={total_entries} files={files_indexed} "
               f"skipped={files_skipped} errors={errors} in {elapsed:.2f}s")
    log_line(summary)
    log_line("run done")
    print(f"[{_utc_now_iso()}] {summary}")
    return 0 if errors == 0 else (0 if total_entries or files_indexed else 1)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def collapse_home(path: str) -> str:
    home = str(Path.home())
    if sys.platform == "win32":
        if path.lower().startswith(home.lower()):
            return "%USERPROFILE%" + path[len(home):]
        return path
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


def parse_duration_or_date(value: str) -> str:
    """Turn '1h'/'2d'/'1w'/'1m' or an ISO date into an ISO timestamp (UTC)."""
    value = value.strip()
    m = re.fullmatch(r"(\d+)\s*([hdwm])", value, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = {
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
            "m": timedelta(days=30 * n),
        }[unit]
        return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    # treat as a date/datetime. entries.ts is stored as UTC ("…Z"), so a
    # bare/naive date or datetime is interpreted as *local* wall-clock and
    # converted to UTC; an explicit offset is honored. This keeps string
    # comparisons against ts correct across timezones.
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise SystemExit(f"Invalid --since/--until value: {value!r}")


def build_fts_query(raw: str) -> str:
    if _FTS_OPERATOR_RE.search(raw):
        return raw  # explicit FTS5 syntax — pass through
    # auto-quote as a single phrase; escape embedded double quotes
    escaped = raw.replace('"', '""')
    return f'"{escaped}"'


def cmd_search(args) -> int:
    conn = connect_ro(Paths.db_path())
    fts_query = build_fts_query(args.query)

    where = ["entries_fts MATCH ?"]
    params: list = [fts_query]

    if args.since:
        where.append("entries.ts >= ?")
        params.append(parse_duration_or_date(args.since))
    if args.until:
        where.append("entries.ts <= ?")
        params.append(parse_duration_or_date(args.until))
    if args.project:
        where.append("entries.project LIKE ?")
        params.append(f"%{args.project}%")
    if args.role:
        where.append("entries.role = ?")
        params.append(args.role)
    if args.kind:
        where.append("entries.kind = ?")
        params.append(args.kind)
    if args.session:
        where.append("entries.session_id LIKE ?")
        params.append(f"{args.session}%")

    snippet_expr = (
        "snippet(entries_fts, 0, '…', '…', '…', 16)"
    )
    sql = (
        f"SELECT entries.id, entries.session_id, entries.project, entries.ts, "
        f"entries.role, entries.kind, entries.tool_name, entries.text, "
        f"{snippet_expr} AS snip "
        f"FROM entries_fts "
        f"JOIN entries ON entries.id = entries_fts.rowid "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY entries.ts DESC "
        f"LIMIT ?"
    )
    params.append(args.limit)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"Search query error: {exc}. (Tip: quote phrases or "
                         f"check FTS5 operator syntax.)")

    if args.json:
        out = []
        for r in rows:
            snip = r["snip"] or ""
            out.append({
                "id": r["id"],
                "session_id": r["session_id"],
                "project": r["project"],
                "ts": r["ts"],
                "role": r["role"],
                "kind": r["kind"],
                "tool_name": r["tool_name"],
                "text": r["text"],
                "snippet": snip,
                "match_count": snip.count("…"),
            })
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print("No results.")
        return 0

    for r in rows:
        ts_disp = _localize(r["ts"])
        proj = collapse_home(r["project"])
        sess = (r["session_id"] or "")[:8]
        header = f"[{ts_disp}] {r['kind']} | {proj} · {sess} #{r['id']}"
        print(header)
        if not args.no_snippet:
            snip = (r["snip"] or "").replace("\n", " ").strip()
            if not snip:
                snip = r["text"].replace("\n", " ")[:SNIPPET_WIDTH]
            print(f"  {snip}")
        print(f"  → cc-history show {r['id']}")
        print()
    return 0


def _localize(ts: str | None) -> str:
    if not ts:
        return "????-??-?? ??:??:??"
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


# ---------------------------------------------------------------------------
# Show
# ---------------------------------------------------------------------------

def cmd_show(args) -> int:
    conn = connect_ro(Paths.db_path())
    focal = conn.execute(
        "SELECT * FROM entries WHERE id = ?", (args.entry_id,)
    ).fetchone()
    if focal is None:
        print(f"No entry with id {args.entry_id}.", file=sys.stderr)
        return 1

    session_id = focal["session_id"]
    rows = conn.execute(
        "SELECT * FROM entries WHERE session_id = ? "
        "ORDER BY ts, line_no, id",
        (session_id,),
    ).fetchall()

    # locate focal index
    ids = [r["id"] for r in rows]
    try:
        pos = ids.index(args.entry_id)
    except ValueError:
        rows = [focal]
        pos = 0

    n = args.context
    lo = max(0, pos - n)
    hi = min(len(rows), pos + n + 1)

    print(f"Session {session_id}  ({collapse_home(focal['project'])})")
    print("=" * 72)
    for i in range(lo, hi):
        r = rows[i]
        marker = "▶ " if r["id"] == args.entry_id else "  "
        ts_disp = _localize(r["ts"])
        tool = f" {r['tool_name']}" if r["tool_name"] else ""
        print(f"{marker}[{ts_disp}] {r['role']}/{r['kind']}{tool} #{r['id']}")
        for line in r["text"].splitlines() or [""]:
            print(f"    {line}")
        print()
    return 0


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def cmd_stats(args) -> int:
    db_path = Paths.db_path()
    conn = connect_ro(db_path)

    projects = conn.execute("SELECT COUNT(DISTINCT project) c FROM entries").fetchone()["c"]
    sessions = conn.execute("SELECT COUNT(DISTINCT session_id) c FROM entries").fetchone()["c"]
    total = conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
    by_kind = conn.execute(
        "SELECT kind, COUNT(*) c FROM entries GROUP BY kind ORDER BY c DESC"
    ).fetchall()
    bounds = conn.execute(
        "SELECT MIN(ts) lo, MAX(ts) hi FROM entries WHERE ts IS NOT NULL"
    ).fetchone()

    db_size = 0
    for ext in ("", "-wal", "-shm"):
        p = Path(str(db_path) + ext)
        if p.exists():
            db_size += p.stat().st_size

    last_run_row = conn.execute("SELECT value FROM meta WHERE key='last_run'").fetchone()

    print(f"Projects:   {projects}")
    print(f"Sessions:   {sessions}")
    print(f"Entries:    {total}")
    print("By kind:")
    for r in by_kind:
        print(f"  {r['kind']:<12} {r['c']}")
    print(f"Earliest:   {_localize(bounds['lo']) if bounds['lo'] else '-'}")
    print(f"Latest:     {_localize(bounds['hi']) if bounds['hi'] else '-'}")
    print(f"DB size:    {_human_bytes(db_size)}  ({db_path})")

    if last_run_row:
        try:
            lr = json.loads(last_run_row["value"])
            print(f"Last run:   {_localize(lr['at'])}  "
                  f"(+{lr['entries']} entries, {lr['files']} files, {lr['elapsed']}s)")
        except (ValueError, KeyError):
            pass
    else:
        print("Last run:   never")

    sched = get_scheduler()
    if sched is not None:
        installed = sched.is_installed()
        print(f"Scheduler:  installed={'yes' if installed else 'no'}")
    else:
        print("Scheduler:  unsupported on this platform")
    return 0


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n} B"


# ---------------------------------------------------------------------------
# Scheduler backends
# ---------------------------------------------------------------------------

class Scheduler:
    def install(self, interval_seconds: int) -> None:
        raise NotImplementedError

    def uninstall(self) -> None:
        raise NotImplementedError

    def is_installed(self) -> bool:
        raise NotImplementedError

    def kickstart(self) -> None:
        raise NotImplementedError

    def status_command_hint(self) -> str:
        raise NotImplementedError

    def install_shim(self) -> str:
        """Create a `cc-history` entry-point so it runs as a bare command, and
        try to put it on PATH. Returns a human-readable hint for the install
        summary. Must never raise — fall back to printing the path."""
        raise NotImplementedError


class MacLaunchAgentScheduler(Scheduler):
    def __init__(self):
        import plistlib  # noqa: F401 — ensure available; imported lazily where used
        self.username = getpass.getuser()
        self.label = f"com.{self.username}.cc-history.indexer"
        self.plist_path = (
            Path.home() / "Library" / "LaunchAgents" / f"{self.label}.plist"
        )

    def _gui_target(self) -> str:
        return f"gui/{os.getuid()}"

    def install(self, interval_seconds: int) -> None:
        import plistlib
        script_path = Path(__file__).resolve()
        plist = {
            "Label": self.label,
            "ProgramArguments": [sys.executable, str(script_path), "index"],
            "StartInterval": interval_seconds,
            "RunAtLoad": True,
            "StandardOutPath": str(Paths.app_data_dir() / "indexer.launchd.log"),
            "StandardErrorPath": str(Paths.app_data_dir() / "indexer.launchd.log"),
        }
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        with self.plist_path.open("wb") as fh:
            plistlib.dump(plist, fh)

        r = subprocess.run(
            ["launchctl", "bootstrap", self._gui_target(), str(self.plist_path)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            combined = (r.stderr or "") + (r.stdout or "")
            if "unsupported" in combined.lower() or "deprecated" in combined.lower():
                subprocess.run(["launchctl", "load", str(self.plist_path)], check=False)
            elif "already" not in combined.lower():
                # surface but don't crash; re-bootstrap after bootout
                subprocess.run(["launchctl", "bootout", f"{self._gui_target()}/{self.label}"],
                               check=False)
                subprocess.run(["launchctl", "bootstrap", self._gui_target(), str(self.plist_path)],
                               check=False)
        subprocess.run(
            ["launchctl", "enable", f"{self._gui_target()}/{self.label}"],
            check=False,
        )

    def uninstall(self) -> None:
        subprocess.run(
            ["launchctl", "bootout", f"{self._gui_target()}/{self.label}"],
            check=False, capture_output=True,
        )
        self.plist_path.unlink(missing_ok=True)

    def is_installed(self) -> bool:
        r = subprocess.run(
            ["launchctl", "list", self.label], capture_output=True, text=True
        )
        return r.returncode == 0

    def kickstart(self) -> None:
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"{self._gui_target()}/{self.label}"],
            check=False,
        )

    def status_command_hint(self) -> str:
        return "launchctl list | grep cc-history"

    def install_shim(self) -> str:
        bin_dir = Path.home() / ".local" / "bin"
        shim = bin_dir / "cc-history"
        script_path = Path(__file__).resolve()
        try:
            bin_dir.mkdir(parents=True, exist_ok=True)
            shim.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                f'os.execv(sys.executable, [sys.executable, "{script_path}"] '
                "+ sys.argv[1:])\n"
            )
            shim.chmod(0o755)
        except OSError as exc:
            return (f"Could not create the cc-history shim ({exc}). Run it as: "
                    f"python3 {script_path} <command>")

        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        if str(bin_dir) not in path_dirs:
            return (f"Created {shim}.\n  ~/.local/bin is not on your PATH — add "
                    f"it (e.g. echo 'export PATH=\"$HOME/.local/bin:$PATH\"' "
                    f">> ~/.zshrc) and reopen your terminal to use 'cc-history' "
                    f"directly.")
        return f"'cc-history' installed to {shim} (already on PATH)."


class WindowsTaskScheduler(Scheduler):
    def __init__(self):
        self.username = os.environ.get("USERNAME") or getpass.getuser()
        self.task_name = f"cc-history\\{self.username}-indexer"

    def install(self, interval_seconds: int) -> None:
        script_path = Path(__file__).resolve()
        interval_seconds = max(MIN_INTERVAL, interval_seconds)
        iso_minutes = max(1, math.ceil(interval_seconds / 60))
        interval_iso = f"PT{iso_minutes}M"

        pythonw = Path(sys.executable).with_name("pythonw.exe")
        runner = pythonw if pythonw.exists() else Path(sys.executable)

        start_boundary = datetime.now().replace(microsecond=0).isoformat()
        domain = os.environ.get("USERDOMAIN")
        user = os.environ.get("USERNAME")
        principal_user = f"{domain}\\{user}" if domain and user else (user or None)

        principals_block = (
            f"""  <Principals>
    <Principal id="Author">
      <UserId>{principal_user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
""" if principal_user else ""
        )

        # A LogonTrigger with no <UserId> means "any user logs on", which
        # Task Scheduler treats as a privileged operation and rejects with
        # "Access is denied" for a non-elevated caller. Scope it to the
        # current user to keep creation unprivileged; omit it entirely if we
        # can't identify the user (the TimeTrigger still keeps the index fresh).
        logon_trigger = (
            f"""    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{principal_user}</UserId>
    </LogonTrigger>
""" if principal_user else ""
        )

        xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>cc-history indexer — keeps the Claude Code transcript FTS index fresh.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>{interval_iso}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start_boundary}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
{logon_trigger}  </Triggers>
{principals_block}  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Hidden>true</Hidden>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{runner}</Command>
      <Arguments>"{script_path}" index</Arguments>
    </Exec>
  </Actions>
</Task>"""

        tmp = Path(tempfile.gettempdir()) / "cc-history-task.xml"
        tmp.write_text(xml, encoding="utf-16")
        try:
            r = subprocess.run(
                ["schtasks", "/Create", "/F", "/TN", self.task_name, "/XML", str(tmp)],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                raise SystemExit(
                    "schtasks failed to create the task. stderr follows so you "
                    "can share it with IT if this is policy-blocked:\n"
                    + (r.stderr or r.stdout or "(no output)")
                )
        finally:
            tmp.unlink(missing_ok=True)

    def uninstall(self) -> None:
        subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", self.task_name],
            check=False, capture_output=True,
        )

    def is_installed(self) -> bool:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", self.task_name],
            capture_output=True, text=True,
        )
        return r.returncode == 0

    def kickstart(self) -> None:
        subprocess.run(
            ["schtasks", "/Run", "/TN", self.task_name],
            check=False, capture_output=True,
        )

    def status_command_hint(self) -> str:
        return f'schtasks /Query /TN "{self.task_name}" /V /FO LIST'

    def install_shim(self) -> str:
        app_dir = Paths.app_data_dir()
        script_path = Path(__file__).resolve()
        cmd_path = app_dir / "cc-history.cmd"
        try:
            app_dir.mkdir(parents=True, exist_ok=True)
            # Interactive use wants visible output, so the shim uses python.exe
            # (sys.executable), not the background pythonw.exe.
            cmd_path.write_text(
                f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n',
                encoding="utf-8",
            )
        except OSError as exc:
            return (f"Could not create the cc-history shim ({exc}). Run it as: "
                    f'"{sys.executable}" "{script_path}" <command>')

        status = self._add_to_user_path(str(app_dir))
        if status == "added":
            return (f"Added {app_dir} to your user PATH. Reopen your terminal, "
                    f"then 'cc-history' works as a bare command.")
        if status == "present":
            return f"'cc-history' available at {cmd_path} (its dir is already on PATH)."
        return (f"'cc-history' is available at:\n  {cmd_path}\n  (could not update "
                f"PATH automatically — add that folder to your PATH manually, or "
                f"call the .cmd by full path.)")

    @staticmethod
    def _add_to_user_path(directory: str) -> str:
        """Add `directory` to the *user* PATH via the registry (never setx, which
        truncates at 1024 chars and merges machine PATH). Returns one of
        'added' | 'present' | 'failed'."""
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as key:
                try:
                    cur, _vtype = winreg.QueryValueEx(key, "Path")
                except FileNotFoundError:
                    cur = ""
                entries = [p for p in cur.split(";") if p]
                if any(os.path.normcase(p) == os.path.normcase(directory)
                       for p in entries):
                    return "present"
                new = f"{cur};{directory}" if cur else directory
                winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new)
            _broadcast_env_change()
            return "added"
        except OSError:
            return "failed"


def _broadcast_env_change() -> None:
    """Tell running shells the environment changed (WM_SETTINGCHANGE)."""
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x1A
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(ctypes.c_ulong()),
        )
    except Exception:  # noqa: BLE001 — best-effort broadcast
        pass


def get_scheduler() -> Scheduler | None:
    if sys.platform == "darwin":
        return MacLaunchAgentScheduler()
    if sys.platform == "win32":
        return WindowsTaskScheduler()
    return None


def _require_scheduler() -> Scheduler:
    sched = get_scheduler()
    if sched is None:
        raise SystemExit("cc-history v1 supports macOS and Windows only.")
    return sched


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------

def cmd_install(args) -> int:
    sched = _require_scheduler()

    interval = args.interval
    if interval < MIN_INTERVAL:
        print(f"Interval {interval}s is below the {MIN_INTERVAL}s minimum; "
              f"raising to {MIN_INTERVAL}s.")
        interval = MIN_INTERVAL

    # 1 + 2: init DB and run a full index synchronously
    init_db(Paths.db_path()).close()
    print("Running initial full index...")
    index_args = argparse.Namespace(full=True, verbose=args.verbose)
    cmd_index(index_args)

    # 3 + 4: hand off to the platform scheduler
    sched.install(interval)
    sched.kickstart()

    # Entry-point shim so `cc-history` works as a bare command (Appendix B).
    shim_hint = sched.install_shim()

    # 5: summary
    print()
    print("cc-history scheduler installed.")
    print(f"  Interval:   {interval}s")
    print(f"  Log:        {Paths.log_path()}")
    print(f"  DB:         {Paths.db_path()}")
    print(f"  Verify:     {sched.status_command_hint()}")
    print(f"  Command:    {shim_hint}")
    print()
    print("Try a search:")
    print('  cc-history search "something you remember" --limit 5')

    if sys.platform == "darwin":
        projects = str(Paths.claude_projects_dir()).lower()
        for prot in ("/desktop/", "/documents/", "/library/mobile documents/"):
            if prot in projects:
                print()
                print("Note: your transcripts resolve under a TCC-protected location; "
                      "macOS may prompt for access on first run.")
                break
    return 0


def cmd_uninstall(args) -> int:
    sched = _require_scheduler()
    sched.uninstall()
    print("cc-history scheduler removed (the index DB was left intact).")
    if sys.platform == "win32":
        print("To remove all data: "
              "Remove-Item -Recurse -Force $env:LOCALAPPDATA\\cc-history")
    else:
        print("To remove all data: rm -rf ~/.cc-history")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SEARCH_EPILOG = """\
Query syntax:
  Multi-word queries are treated as a single phrase by default:
      cc-history search auth flow          ->  matches the phrase "auth flow"
  Use explicit FTS5 operators for advanced queries (detected automatically
  when any of " * ( ) : AND OR NOT NEAR appear):
      cc-history search 'auth OR login'
      cc-history search '"exact phrase"'
      cc-history search 'data*'            ->  prefix match
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cc-history",
        description="Local full-text search over Claude Code session transcripts.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("index", help="Index transcripts into the FTS database.")
    pi.add_argument("--full", action="store_true",
                    help="Drop and rebuild the index from scratch.")
    pi.add_argument("--verbose", action="store_true", help="Per-file progress.")
    pi.set_defaults(func=cmd_index)

    ps = sub.add_parser("search", help="Search the index.",
                        epilog=SEARCH_EPILOG,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    ps.add_argument("query", help="Search query (phrase by default).")
    ps.add_argument("--since", help="1h | 2d | 1w | 1m | 2026-01-15")
    ps.add_argument("--until", help="1h | 2d | 1w | 1m | 2026-01-15")
    ps.add_argument("--project", help="Filter by project path or basename (LIKE).")
    ps.add_argument("--role", choices=["user", "assistant"], help="Filter by role.")
    ps.add_argument("--kind", choices=["text", "thinking", "tool_use", "tool_result"],
                    help="Filter by content kind.")
    ps.add_argument("--session", help="Session UUID or prefix (8 chars enough).")
    ps.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"Max results (default {DEFAULT_LIMIT}).")
    ps.add_argument("--json", action="store_true", help="Machine-readable output.")
    ps.add_argument("--no-snippet", action="store_true", help="Metadata only.")
    ps.set_defaults(func=cmd_search)

    psh = sub.add_parser("show", help="Show an entry with surrounding context.")
    psh.add_argument("entry_id", type=int, help="Entry id (from search output).")
    psh.add_argument("--context", type=int, default=5,
                     help="Entries of context before/after (default 5).")
    psh.set_defaults(func=cmd_show)

    pst = sub.add_parser("stats", help="Show index statistics.")
    pst.set_defaults(func=cmd_stats)

    pin = sub.add_parser("install", help="Install the background indexer scheduler.")
    pin.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                     help=f"Seconds between runs (default {DEFAULT_INTERVAL}, "
                          f"min {MIN_INTERVAL}).")
    pin.add_argument("--verbose", action="store_true")
    pin.set_defaults(func=cmd_install)

    pun = sub.add_parser("uninstall", help="Remove the scheduler (keeps the DB).")
    pun.set_defaults(func=cmd_uninstall)

    return p


def _force_utf8_stdio() -> None:
    """Windows consoles default to a legacy codepage (cp1252) that can't encode
    '·', '…' or Hebrew. Reconfigure stdio to UTF-8 so output never crashes."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
