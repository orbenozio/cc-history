# `cc-history` — Implementation Spec

A local full-text search tool for Claude Code session transcripts. Anyone running Claude Code on **macOS or Windows** can install it to search across every conversation they've ever had on their machine, filtered by project, date, role, and content type.

This spec is self-contained — hand it to a fresh Claude session and it should have everything needed to build the tool from scratch on **both** platforms in a single pass.

> **Revision note (rev. 2026-05-31):** the Windows-specific details — project-folder encoding (§6.2), Task Scheduler XML / principal / interval handling (§8b), and the PATH-update mechanism (Appendix B) — were corrected against a real Claude Code install on Windows 11. Key correction: Windows folder names do **not** carry a leading `-`, and a drive root like `c:\` encodes to `c--`.

***

## 1. Goal & non-goals

**Goal**: Index Claude Code's local JSONL transcripts (`~/.claude/projects/**/*.jsonl`) into a SQLite+FTS5 database, expose a CLI for searching, and keep the index fresh via a platform-native background scheduler (macOS LaunchAgent, Windows Task Scheduler).

**Non-goals (v1)**:

* No cloud sync; no remote indexing.
* No embeddings / no semantic search — FTS5 only.
* No "transcript-surviving-deletion" archival (designed-for, see §12).
* No GUI / no web UI.
* **No Linux support in v1.** Code keeps platform-specific bits isolated behind a `Scheduler` interface and a `Paths` helper, so Linux (`systemd --user`) can be added later as a third backend without rewriting the indexer.

## 2. Prior art

A Go tool called `sift` (github.com/hrishikeshs/sift) does roughly the same thing on macOS. Worth a look for UX choices (CLI flags, output format, the `show <id>` interaction). `cc-history` differs by being:

* Python stdlib only — no Go toolchain, no `go install`, no extra binaries to ship.
* Single-file core (`cc_history.py`), easy to hack.
* Cross-platform from day one: macOS LaunchAgent + Windows Task Scheduler.

Don't replicate sift's archival/compression behavior in v1; that's a v2 item (§12).

## 3. Architecture

Three logical components, one Python script + thin platform-specific install scripts:

1. **Indexer** — `cc-history index [--full]`. Walks Claude Code's projects dir, parses .jsonl files, inserts into SQLite. Incremental by default (resumes from byte offset per file). Each file processed inside a single SQLite transaction so a crash mid-file cannot produce duplicates.
2. **Searcher** — `cc-history search <query>` and `cc-history show <id>`. Reads from the DB, formats results.
3. **Scheduler installer** — `cc-history install` / `cc-history uninstall`. Dispatches to one of two backends:
   * **macOS**: generates a plist, loads it via `launchctl`.
   * **Windows**: registers a Task Scheduler task via `schtasks.exe`.

Both backends invoke the same `cc-history index` command on a fixed interval.

### 3.1 Platform abstraction

All platform-specific code lives behind two small modules inside `cc_history.py`:

```Python
class Paths:
    @staticmethod
    def claude_projects_dir() -> Path: ...   # ~/.claude/projects on both platforms
    @staticmethod
    def app_data_dir() -> Path: ...          # ~/.cc-history  on Mac/Linux
                                             # %LOCALAPPDATA%\cc-history on Windows
    @staticmethod
    def db_path() -> Path: ...
    @staticmethod
    def log_path() -> Path: ...

class Scheduler:
    """Abstract base. Subclassed by MacLaunchAgentScheduler and WindowsTaskScheduler."""
    def install(self, interval_seconds: int) -> None: ...
    def uninstall(self) -> None: ...
    def is_installed(self) -> bool: ...
    def kickstart(self) -> None: ...   # run now, out-of-band
    def status_command_hint(self) -> str: ...  # for the install summary

def get_scheduler() -> Scheduler:
    if sys.platform == "darwin": return MacLaunchAgentScheduler()
    if sys.platform == "win32":  return WindowsTaskScheduler()
    raise SystemExit("cc-history v1 supports macOS and Windows only.")
```

Everything else (indexer, searcher, FTS, JSONL parsing, CLI) is platform-neutral and goes through `Paths` / `Scheduler`.

**Size guidance**: expect \~600-800 lines for `cc_history.py` including both Scheduler backends. Stdlib only (`sqlite3`, `argparse`, `json`, `pathlib`, `plistlib`, `subprocess`, `os`, `sys`, `getpass`, `datetime`, `re`, `shutil`, `tempfile`).

## 4. Filesystem layout

### 4.1 macOS

```
~/.cc-history/
├── index.db            # SQLite with FTS5 (WAL mode)
├── index.db-wal
├── index.db-shm
└── indexer.log         # written by the Python script itself; rotated by size

~/Library/LaunchAgents/
└── com.<username>.cc-history.indexer.plist
```

### 4.2 Windows

```
%LOCALAPPDATA%\cc-history\         e.g. C:\Users\<name>\AppData\Local\cc-history
├── index.db
├── index.db-wal
├── index.db-shm
└── indexer.log

Task Scheduler:
\cc-history\<username>-indexer     (registered task; visible in taskschd.msc)
```

`Paths.app_data_dir()` is the single source of truth. Resolve via:

* macOS / Linux: `Path.home() / ".cc-history"`
* Windows: `Path(os.environ["LOCALAPPDATA"]) / "cc-history"` (fall back to `Path.home() / ".cc-history"` if `LOCALAPPDATA` is unset, which shouldn't happen on a normal install).

Source of truth for transcripts: `Path.home() / ".claude" / "projects"`. This is the same on macOS and Windows because Claude Code itself uses `os.homedir()`/`Path.home()`.

## 5. Data model

```SQL
PRAGMA journal_mode = WAL;       -- critical: lets searches read while indexer writes
PRAGMA synchronous  = NORMAL;    -- WAL-safe and faster than FULL
PRAGMA foreign_keys = ON;

-- Per-file tracking for incremental indexing.
CREATE TABLE files (
    path TEXT PRIMARY KEY,           -- absolute path to .jsonl (platform-native separators)
    project TEXT NOT NULL,           -- decoded human-readable project path
    session_id TEXT,                 -- read from JSONL contents
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    last_offset INTEGER NOT NULL,    -- byte offset of last consumed line
    last_indexed_at TEXT NOT NULL    -- ISO 8601
);

CREATE INDEX idx_files_session ON files(session_id);
CREATE INDEX idx_files_project ON files(project);

-- One row per indexable JSONL entry.
CREATE TABLE entries (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    project TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    ts TEXT,                         -- ISO 8601 from JSONL; null if missing
    role TEXT,                       -- 'user' | 'assistant' | 'system'
    kind TEXT NOT NULL,              -- 'text' | 'thinking' | 'tool_use' | 'tool_result'
    tool_name TEXT,                  -- set when kind='tool_use'
    text TEXT NOT NULL
);

CREATE INDEX idx_entries_session ON entries(session_id);
CREATE INDEX idx_entries_project_ts ON entries(project, ts);
CREATE INDEX idx_entries_kind ON entries(kind);

-- FTS5 virtual table mirroring entries.text.
CREATE VIRTUAL TABLE entries_fts USING fts5(
    text,
    content='entries',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER entries_ai AFTER INSERT ON entries BEGIN
  INSERT INTO entries_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER entries_ad AFTER DELETE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
```

`unicode61 remove_diacritics 2` ensures Hebrew and accented Latin text tokenize sensibly. Entries are append-only (no UPDATE trigger needed).

### 5.1 Concurrency rules

* Always open the DB with `PRAGMA journal_mode=WAL` at first connect.
* The indexer takes a single connection and processes one file per transaction. Open `BEGIN IMMEDIATE`, insert all entries from the file plus the `files` row update, then `COMMIT`. On any error mid-file, rollback and don't advance `last_offset`.
* The searcher opens a separate read-only connection (`sqlite3.connect(uri=True, ...?mode=ro')`).

## 6. JSONL parsing rules

Each `.jsonl` file is one JSON object per line. Shapes observed in real transcripts:

* `{"type": "queue-operation", ...}` — internal scheduling noise. **SKIP.**
* `{"type": "summary", ...}` — context compaction checkpoints. **SKIP in v1.**
* `{"type": "user", "message": <obj or stringified obj>, "timestamp": "...", "sessionId": "...", "cwd": "...", ...}` — a user turn or a tool result.
* `{"type": "assistant", "message": {...}, "timestamp": "...", "sessionId": "...", ...}` — a model response.

For user/assistant entries, the payload is in `message.content`, an array of blocks. Each block has its own `type`:

| Block `type`  | Outer role  | `kind`        | Text to index                                                         | Notes                                                      |
| ------------- | ----------- | ------------- | --------------------------------------------------------------------- | ---------------------------------------------------------- |
| `text`        | as outer    | `text`        | `block.text`                                                          |                                                            |
| `thinking`    | `assistant` | `thinking`    | `block.thinking`                                                      | Indexed by default. See §6.1.                              |
| `tool_use`    | `assistant` | `tool_use`    | `f"{block.name}({json.dumps(block.input)})"`, truncated to 4096 **bytes** (UTF-8) | `tool_name = block.name`                        |
| `tool_result` | `user`      | `tool_result` | Stringified `block.content`, truncated to 8192 **bytes** (UTF-8)      | If truncated, append `\n[…truncated, full length N bytes]` |
| `image`       | —           | —             | SKIP                                                                  |                                                            |

### 6.1 Robustness rules

* `message` may be a string (older transcripts serialized it). Try `json.loads`; if that fails, store the raw string as a single `text` entry with `role` from the outer object.
* Missing `timestamp` → fall back to file mtime, note in `indexer.log` once per file, still index.
* Missing `sessionId` → fall back to the filename's UUID stem.
* Malformed line (JSON decode error) → log to indexer.log, increment a per-file counter, skip the line **but continue reading subsequent lines**. After 10 malformed lines in a single file, give up on that file for this run and log loudly. Because everything from that file is in one transaction, abandoning the file rolls back any rows already inserted from it this run; `last_offset` stays at its prior value so the next run retries from the same point.
* Files modified mid-read: re-stat at end; if size grew, the next incremental run picks up the new bytes.

### 6.2 Project name decoding (cross-platform)

Folder names under `~/.claude/projects/` are encoded paths. The encoding rule is uniform across platforms: **Claude Code replaces every occurrence of** **`/`,** **`\`,** **`:`,** **`.`, and** **`_`** **with** **`-`.** There is no separately-added leading `-`; macOS paths merely *look* like they have one because they start with `/`. The original drive-letter casing is preserved on Windows.

Verified examples (confirmed against a real Claude Code install):

* macOS: `/Users/orbenozio/Candivore_Unity6000.3.7f1` → `-Users-orbenozio-Candivore-Unity6000-3-7f1` (leading `-` is just the encoded leading `/`).
* Windows: `c:\Users\orben\OneDrive\DEV\Projects\Diburit` → `c--Users-orben-OneDrive-DEV-Projects-Diburit`. Note `c:\` → `c--` because **both** the `:` and the `\` collapse to `-`, and the drive letter keeps its original case (lowercase `c` here). Windows folder names therefore do **not** start with `-`.

The encoding is **lossy** (`/`, `\`, `:`, `.`, `_` all collapse to `-`, and case of the drive letter may vary by how `cwd` was recorded). Decoding heuristic, in order of preference:

1. **`cwd`** **fast path (strongly preferred)** — scan the first few JSONL entries for a `cwd` field. The transcripts record it verbatim (e.g. `"cwd":"c:\\Users\\orben\\OneDrive\\DEV\\Projects\\Diburit"`). If `cwd` is an existing directory on disk, use it verbatim and skip the heuristic entirely. This sidesteps every lossiness problem and is correct in the overwhelming majority of cases.
2. **Greedy resolution against the live filesystem** (only when no usable `cwd` is found):
   * Split the folder name on `-`.
   * On macOS/Linux: the name starts with `-`, so the first split element is empty; start the reconstruction at `/<part1>` using the first non-empty part.
   * On Windows: the name starts with the drive letter. The first part is a single letter, immediately followed by an empty part (from the collapsed `:` then `\`); reconstruct the root as `<part1>:\` and continue from the next non-empty part.
   * For each subsequent part, try appending in this priority order, picking the first that resolves to an existing path: `<platform-sep><part>` (`/` or `\`), `.<part>`, `_<part>`. Pick the longest existing prefix.
   * Existence checks must be **case-insensitive on Windows** (the FS is case-insensitive; the encoded name may not match the on-disk casing).
3. **Naive fallback** — if nothing on disk resolves (project was renamed/deleted/on another machine), substitute `-` → `/` on Mac/Linux or `-` → `\` on Windows and store as-is. On Windows, additionally rewrite a leading `<letter>--` back to `<letter>:\`. Mark `files.project` exactly this way; don't crash.

Store the resolved string in `files.project` and propagate to `entries.project` for every row from that file.

> Implementation note: because the `:` in a Windows drive root collapses to a dash that is indistinguishable from a path-separator dash, the greedy reconstruction can never recover `c:\` unambiguously from the folder name alone. This is exactly why the `cwd` fast path is mandatory-first rather than a mere optimization.

## 7. CLI surface

```
cc-history index [--full] [--verbose]
cc-history search <query> [options]
cc-history show <entry-id> [--context N]
cc-history stats
cc-history install [--interval SECONDS]
cc-history uninstall
```

### 7.1 `index`

* Default: incremental. For each `.jsonl` under the Claude projects dir:
  * If `(path, mtime_ns, size)` matches the `files` row, skip.
  * Else open at `last_offset`, read remaining bytes, parse line by line, insert + update `files` row in a single transaction.
* `--full`: drop `entries`, `entries_fts`, and `files`; rebuild from scratch.
* `--verbose`: print per-file progress.
* Exit code 0 on success, 1 on hard error (DB inaccessible, projects dir missing).
* Always writes a one-line summary to `indexer.log`: `[ISO-ts] indexed N entries from M files in T.Ts (skipped K unchanged)`.

### 7.2 `search`

```
cc-history search <query>
  [--since DURATION_OR_DATE]    # 1h, 2d, 1w, 1m, 2026-01-15
  [--until DURATION_OR_DATE]
  [--project PATH_OR_BASENAME]  # matches files.project via LIKE
  [--role user|assistant]
  [--kind text|thinking|tool_use|tool_result]
  [--session UUID_OR_PREFIX]    # 8-char prefix sufficient
  [--limit N]                    # default 20
  [--json]                       # machine-readable
  [--no-snippet]                 # metadata only
```

**Query syntax**: multi-word queries are auto-quoted as a single phrase. Explicit FTS5 operators (`AND`, `OR`, `NOT`, `"phrase"`, `prefix*`) pass through if any FTS5 operator character is detected in the raw query. `--help` documents both modes with examples.

**Default text output** (one result per 3-line block):

```
[2026-05-15 14:23:07] thinking | ~/Projects/foo · 9620f8ab #58611
  …the heatmap component needs to re-render when filter changes, which means…
  → cc-history show 58611
```

* Timestamp: localized.
* Snippet: 120 chars around the first FTS hit (use FTS5 `snippet(...)` with `…` markers).
* Sort: most recent first by default.
* Project path display: `~` collapse on macOS; `%USERPROFILE%` collapse on Windows.

**JSON output**: array of objects with all entry fields plus `snippet`, `match_count`.

### 7.3 `show`

```
cc-history show <entry-id> [--context N]   # default N=5
```

Print the full text of entry `<id>`, plus N entries before and after **in the same session**, ordered chronologically. Highlight the focal entry (prefix with `▶`).

### 7.4 `stats`

Print: number of projects, sessions, entries by kind, earliest/latest entry timestamp, total DB size on disk, last indexer run timestamp + duration + entries added, scheduler status (`installed: yes/no` + interval).

### 7.5 `install`

```
cc-history install [--interval SECONDS]   # default 600; clamped to a 60s minimum
```

Interval is clamped to **>= 60 seconds** on both platforms (Task Scheduler can't reliably repeat faster than once a minute, and sub-minute indexing has no practical benefit). If the user passes a smaller value, raise it and print a one-line warning.

1. Initialize DB (`app_data_dir()`) if missing.
2. Run a full index synchronously and report stats.
3. Resolve absolute paths: `python_exe = sys.executable`, `script_path = Path(__file__).resolve()`.
4. Hand off to the platform `Scheduler.install(interval)` (see §8a / §8b).
5. Print: confirmation + sample query command + log path + the `status_command_hint()` so the user can verify.

### 7.6 `uninstall`

1. `Scheduler.uninstall()` — idempotent; succeeds even if not installed.
2. **Do not** delete the app data dir. Print a hint with the platform-appropriate manual-cleanup command:
   * macOS: `rm -rf ~/.cc-history`
   * Windows: `Remove-Item -Recurse -Force $env:LOCALAPPDATA\cc-history`

## 8. Scheduler backends

### 8a. macOS — LaunchAgent

Generate a plist at `~/Library/LaunchAgents/com.<username>.cc-history.indexer.plist` via `plistlib.dump`. Use `getpass.getuser()` for the label namespace.

```XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.{username}.cc-history.indexer</string>
  <key>ProgramArguments</key>
  <array>
    <string>{ABS_PATH_TO_python3}</string>          <!-- sys.executable -->
    <string>{ABS_PATH_TO_cc_history.py}</string>
    <string>index</string>
  </array>
  <key>StartInterval</key><integer>{interval_seconds}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{HOME}/.cc-history/indexer.launchd.log</string>
  <key>StandardErrorPath</key><string>{HOME}/.cc-history/indexer.launchd.log</string>
</dict>
</plist>
```

Use `sys.executable` directly (not `/usr/bin/env python3`) so the agent doesn't depend on the PATH it inherits. The Python script also writes its own `indexer.log` (§9); the `indexer.launchd.log` only catches truly uncaught crashes.

**Install**:

```Python
subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist_path], check=True)
subprocess.run(["launchctl", "enable",    f"gui/{os.getuid()}/{label}"], check=False)
```

**Kickstart (run now)**:

```Python
subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], check=False)
```

**Uninstall**:

```Python
subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], check=False)
plist_path.unlink(missing_ok=True)
```

**Status hint**: `launchctl list | grep cc-history`.

`bootstrap`/`bootout` are the modern API; on older macOS they fall back to `launchctl load`/`launchctl unload` — wrap accordingly if `bootstrap` exits non-zero with the `unsupported` message.

### 8b. Windows — Task Scheduler

Use `schtasks.exe` (always present on Windows). Task name: `cc-history\{username}-indexer`. Use `pythonw.exe` (sibling of `sys.executable`) so no console window flashes; fall back to `sys.executable` if `pythonw.exe` is missing.

**Interval clamping**: Task Scheduler's repetition interval has a practical minimum of **1 minute**; values below that are silently ignored or rejected. Clamp `interval_seconds` to `>= 60` (round up to the nearest minute when emitting the `PT…M`/`PT…S` duration) and warn the user if their requested value was raised. The v1 default of 600 s is unaffected.

**Resolving the principal** **`UserId`**: do **not** leave this as a placeholder — an invalid/empty `UserId` makes `/Create /XML` fail. Use the current interactive user. Prefer `os.environ["USERDOMAIN"] + "\\" + os.environ["USERNAME"]`; fall back to `os.environ.get("USERNAME")` alone, and if even that is missing, omit the entire `<Principals>` block (schtasks then defaults to the registering user). Never hard-code a SID.

**`StartBoundary`**: must be a syntactically valid local datetime but the actual value is irrelevant when `StartWhenAvailable` is true and a `Repetition` is set. Generate it dynamically from "now" rather than hard-coding a date — e.g. `datetime.now().replace(microsecond=0).isoformat()` — so the task is never created with a boundary that looks stale/suspicious to admins.

**`LogonTrigger` must be scoped to a user** (verified 2026-05-31 on Windows 11): a `<LogonTrigger>` with **no** `<UserId>` child means "any user logs on", which Task Scheduler treats as a privileged operation and rejects with `ERROR: Access is denied.` for a non-elevated `/Create /XML`. Add `<UserId>{principal_user}</UserId>` inside the `LogonTrigger` to scope it to the current user (creation then stays unprivileged). If `principal_user` is unknown, omit the `LogonTrigger` entirely — the `TimeTrigger` + `StartWhenAvailable` still keeps the index fresh.

**Install** — build the command, write it to a temp `.xml` for reliability with quoted paths, register via `/Create /XML`:

```Python
import os
from datetime import datetime, timedelta
import math

interval_seconds = max(60, interval_seconds)          # clamp (warn caller if raised)
iso_minutes = max(1, math.ceil(interval_seconds / 60))
interval_iso = f"PT{iso_minutes}M"                      # Task Scheduler is happiest with whole minutes

pythonw = Path(sys.executable).with_name("pythonw.exe")
runner  = pythonw if pythonw.exists() else Path(sys.executable)

start_boundary = datetime.now().replace(microsecond=0).isoformat()
domain = os.environ.get("USERDOMAIN")
user   = os.environ.get("USERNAME")
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
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{principal_user}</UserId>
    </LogonTrigger>
  </Triggers>
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

# Save and register
tmp = Path(tempfile.gettempdir()) / "cc-history-task.xml"
tmp.write_text(xml, encoding="utf-16")
subprocess.run(["schtasks", "/Create", "/F", "/TN", task_name, "/XML", str(tmp)], check=True)
tmp.unlink(missing_ok=True)
```

`/F` overwrites any prior task with the same name (so re-installing with a new interval just works). `InteractiveToken` means the task runs as the logged-in user without storing a password.

**Kickstart (run now)**:

```Python
subprocess.run(["schtasks", "/Run", "/TN", task_name], check=False)
```

**Status**:

```Python
subprocess.run(["schtasks", "/Query", "/TN", task_name], check=False)
```

Returns non-zero exit if the task doesn't exist — use that as `is_installed()`.

**Uninstall**:

```Python
subprocess.run(["schtasks", "/Delete", "/F", "/TN", task_name], check=False)
```

**Status hint**: `schtasks /Query /TN "cc-history\{username}-indexer" /V /FO LIST` or the GUI at `taskschd.msc`.

### 8c. Permission notes (call out to the user during `install`)

* **macOS**: first run may trigger TCC prompts if Claude Code's transcripts are under a TCC-protected location (Desktop / Documents / iCloud). They live under `~/.claude/` by default, which is not protected, so usually no prompt — but mention it in the install summary if `~/.claude/projects/` resolves into one of those paths.
* **Windows**: `LeastPrivilege` + `InteractiveToken` avoids the elevation prompt entirely. If the user is on a corporate-managed machine, Task Scheduler creation can be blocked by Group Policy — surface `schtasks` stderr verbatim if it fails so they have something actionable to share with IT.

## 9. Logging

The Python script writes its own structured log to `app_data_dir() / "indexer.log"` regardless of platform. **Do not rely on shell stdout redirection** — Windows Task Scheduler doesn't redirect natively, and writing it in Python keeps both backends identical.

Log format: one line per run + per-error.

```
2026-05-31T14:00:00Z run start interval=600
2026-05-31T14:00:01Z indexed entries=42 files=3 skipped=187 in 0.74s
2026-05-31T14:00:01Z run done
```

**Rotation**: at the start of each run, if the file is larger than 5 MB, rename it to `indexer.log.1` (overwrite any previous `.1`) and truncate the live file.

The plist's `StandardOutPath` and any failed Task Scheduler output land in `indexer.launchd.log` / Task History — that's only for catastrophic-startup-failure diagnosis, not regular operation.

## 10. Repo layout

```
cc-history/
├── README.md
├── LICENSE                        # MIT
├── cc_history.py                  # the entire tool, one file (~600-800 lines)
├── install.sh                     # macOS/Linux thin wrapper
├── install.ps1                    # Windows thin wrapper
├── .gitignore                     # __pycache__, *.pyc
└── tests/
    ├── fixtures/
    │   └── sample-session.jsonl   # 5-10 lines covering each block type
    └── test_indexer.py            # uses tmpfile DB; platform-independent
```

### 10.1 `install.sh` (macOS)

```Shell
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || { echo "Python 3.9+ required."; exit 1; }
python3 "$SCRIPT_DIR/cc_history.py" install "$@"
```

### 10.2 `install.ps1` (Windows)

```PowerShell
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction Stop }
& $py.Source -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.9+ required." }
& $py.Source "$ScriptDir\cc_history.py" install @args
```

### 10.3 README structure

1. **What it is** — 2 sentences.
2. **Install** — two code blocks side by side: macOS (`./install.sh`) and Windows (`.\install.ps1`).
3. **Usage** — 4-5 example commands with expected output (commands are identical on both platforms once installed).
4. **How it works** — link to this spec.
5. **Uninstall** — `cc-history uninstall` (same on both); plus the manual-cleanup hint for the DB.
6. **Privacy note** — all data stays local, DB at `~/.cc-history/index.db` (Mac) or `%LOCALAPPDATA%\cc-history\index.db` (Windows), nothing leaves the machine.
7. **License** — MIT.

## 11. Sanity tests for the builder

After implementation, these must all pass. Test 1-5 + 9 are platform-independent; 6a/7a are macOS-only; 6b/7b are Windows-only.

1. `cc-history index` completes without errors; second run reports 0 new entries.
2. `cc-history stats` shows non-zero counts for at least `text` and `tool_use` kinds.
3. `cc-history search "<a phrase the user remembers>"` returns at least one hit.
4. `cc-history search foo --json | python3 -m json.tool` is valid JSON.
5. `cc-history show <id>` returns the entry plus surrounding context.
6. Scheduler is registered after `install`:
   * **a (macOS)**: `launchctl list | grep cc-history` returns a row.
   * **b (Windows)**: `schtasks /Query /TN "cc-history\<username>-indexer"` returns the task with `Status: Ready` or `Running`.
7. Kickstart works:
   * **a (macOS)**: `launchctl kickstart -k gui/$(id -u)/com.${USER}.cc-history.indexer` → `indexer.log` shows a new run line within \~5 seconds.
   * **b (Windows)**: `schtasks /Run /TN "cc-history\<username>-indexer"` → `indexer.log` shows a new run line within \~5 seconds.
8. `cc-history uninstall` removes the scheduler entry; the DB at `app_data_dir()/index.db` remains intact.
9. Hebrew search works: `cc-history search "שלום"` returns hits if the user has any Hebrew transcripts, demonstrating that the FTS tokenizer handles non-Latin scripts.
10. Crash-resilience smoke: kill the indexer mid-file (`SIGKILL` on Mac, end-task on Windows), re-run, verify entry counts didn't double and that the killed file was retried from its prior `last_offset`.

## 12. v2 / stretch (do not build in v1, but design with these in mind)

* **Archival**: on first sight of a `.jsonl`, copy it gzipped into `app_data_dir() / "archive" / "YYYY-MM-DD" / "<session-id>.jsonl.gz"` so conversations survive if Claude Code rotates them away.
* **`config.toml`**: `interval_seconds`, `index_thinking` (bool), `tool_result_max_bytes`, custom `claude_projects_path`, custom `db_path`.
* **Linux support**: third `Scheduler` backend emitting a `systemd --user` unit when `sys.platform.startswith('linux')`. Everything else already platform-neutral, so this is a single-file addition.
* **`cc-history serve`**: tiny `http.server`-based browser UI on `localhost:7474`.
* **Skill wrapper for Claude Code**: ship a `.claude/skills/recall/SKILL.md` that wraps `cc-history search --json` so the model can answer "what did we discuss about X" without burning tokens.
* **Cross-machine sync**: optional rclone hook to push `index.db` to a private bucket so search works across multiple machines.

## 13. Defaults locked in v1

* Indexer interval: **600 seconds** (both platforms); user-supplied values clamped to a **60 s minimum**.
* `thinking` blocks: **indexed**.
* `tool_result` content: indexed with **8 KB** truncation per entry.
* `tool_use` content: indexed with **4 KB** truncation per entry.
* DB at `~/.cc-history/index.db` (Mac) / `%LOCALAPPDATA%\cc-history\index.db` (Windows).
* Default search limit: **20** results.
* Default snippet width: **120 chars** with ellipsis markers.
* SQLite: WAL journal, NORMAL sync.

These are v1 defaults — surface as config options later (§12) but don't gate v1 on them.

***

## Appendix A — minimal end-to-end smoke session

### macOS

```Shell
git clone https://github.com/<you>/cc-history.git ~/Projects/cc-history
cd ~/Projects/cc-history
./install.sh

cc-history search "Match3" --limit 5
cc-history search "auth flow" --since 2w --kind thinking
cc-history show 1234 --context 3

launchctl list | grep cc-history
tail -f ~/.cc-history/indexer.log
cc-history stats
```

### Windows (PowerShell)

```PowerShell
git clone https://github.com/<you>/cc-history.git $HOME\Projects\cc-history
cd $HOME\Projects\cc-history
.\install.ps1

cc-history search "Match3" --limit 5
cc-history search "auth flow" --since 2w --kind thinking
cc-history show 1234 --context 3

schtasks /Query /TN "cc-history\$env:USERNAME-indexer"
Get-Content -Wait "$env:LOCALAPPDATA\cc-history\indexer.log"
cc-history stats
```

## Appendix B — entry-point packaging note (both platforms)

`cc-history` as a bare command (not `python3 cc_history.py ...`) is achieved by either:

* (Recommended for v1) generating a tiny shim during `install`:
  * macOS: `~/.local/bin/cc-history` (a `#!/usr/bin/env python3` one-liner that `exec`s the script). Ensure `~/.local/bin` is on PATH; if not, print a hint.
  * Windows: `%LOCALAPPDATA%\cc-history\cc-history.cmd` (`@echo off` + `"<sys.executable>" "<script>" %*`).
    **PATH update — do not use** **`setx PATH "%PATH%;…"`.** `setx` truncates at 1024 characters and `%PATH%` is the *merged* machine+user PATH, so that idiom both corrupts long PATHs and copies machine entries into the user PATH. Instead, read and write the **user** PATH only, via the registry, and skip if the entry is already present:
    ```PowerShell
    $dir  = "$env:LOCALAPPDATA\cc-history"
    $cur  = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($cur -split ';') -notcontains $dir) {
        $new = if ([string]::IsNullOrEmpty($cur)) { $dir } else { "$cur;$dir" }
        [Environment]::SetEnvironmentVariable("Path", $new, "User")
    }
    ```
    (`[Environment]::SetEnvironmentVariable(..., "User")` writes `HKCU\Environment` with no length cap and broadcasts `WM_SETTINGCHANGE`.) The change takes effect in new shells; tell the user to reopen their terminal. If editing PATH fails or is policy-blocked, fall back to printing the full path to `cc-history.cmd` so the tool is still usable.
* (v2) packaging as a real `pyproject.toml` with `[project.scripts] cc-history = "cc_history:main"` and `pipx install .`. Out of scope for v1 — the shim is enough.

