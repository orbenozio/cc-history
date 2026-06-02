# `cc-history` for VS Code — Implementation Spec

A VS Code extension that brings full-text search over Claude Code session transcripts directly into the editor. Built on top of the existing `cc-history` CLI project (same data model, same JSONL parsing rules, same on-disk index), but **self-contained**: it does not require Python or the CLI to be installed.

This spec is self-contained. Hand it to a fresh agent and it should be buildable in a single pass on **macOS and Windows** (the two platforms the author runs). It assumes familiarity with — and reuse of — the CLI spec at `cc-history-spec.md`; sections of that spec are referenced rather than copied.

> Companion document: `cc-history-spec.md` (the CLI). This extension MUST stay compatible with that spec's §5 data model and §6 JSONL parsing rules. Where this document says "see CLI §X", it means that exact section.

***

## 1. Goal & non-goals

### Goal

Let a developer search across every Claude Code conversation on their machine from inside VS Code — full-text, filtered by project, date, role, and content kind — and read any hit with surrounding conversation context, without leaving the editor and without installing anything beyond the extension itself.

### v1 scope

- Full-text search over `~/.claude/projects/**/*.jsonl`, indexed into a SQLite+FTS5 database compatible with the CLI's schema.
- Self-contained TypeScript/Node indexer + searcher (no Python dependency).
- Search UI (Quick Pick driven), a results TreeView in an Activity Bar view, and a read-only context viewer.
- Filters: project, date range, role, kind.
- Incremental background indexing on activation + on an interval, off the UI thread, with unobtrusive progress.
- Configuration surface (`contributes.configuration`).
- Hebrew/RTL-correct rendering of results and context (the author searches Hebrew transcripts).
- Cross-platform (macOS + Windows), including the §6.2 project-name decoding.
- Publishable to the VS Code Marketplace: manifest, icon, README, bundled, signed `.vsix`, CI.

### Non-goals (v1)

- **No semantic search / embeddings** — FTS5 only (matches the CLI).
- **No editing or deleting** transcripts. Read-only over Claude Code's data.
- **No cloud sync, no telemetry, no network calls** of any kind. Everything stays local.
- **No Linux** as a *tested* target in v1 (the path/decoding code is platform-neutral, so Linux should mostly work; it just isn't a validated/listed platform). The Marketplace listing will not claim Linux support until tested.
- **No `win32-arm64`** in v1: the CI native-binary matrix does **not** include Windows on ARM, so those users get **no install** (a deliberate scope cut, not a silent omission). Revisit when there is demand.
- **Remote-SSH / WSL is supported architecturally from v1, but *Remote testing* is deferred to a later phase.** The author sometimes runs Claude Code inside WSL / Remote-SSH, so `~/.claude` can live on the remote/workspace side. The extension is therefore designed to run **workspace-side** (see §7 `extensionKind`) so it reads the correct home directory. The path-resolution seam (§11) must be correct for Remote-SSH/WSL on day one — this is architecturally load-bearing and breaking to change after ship — even though the validated Remote *test pass* lands in Phase 3.
- **No web / Codespaces (browser) host** in v1: the extension requires a Node extension host and a native module, so `virtualWorkspaces` is unsupported (see §7).
- **No archival** of rotated-away transcripts (CLI §12 v2 item).
- **No write-back to the CLI's scheduler** — the extension does its own indexing; it does not install or manage LaunchAgents / Task Scheduler tasks.

### v2 / stretch (design for, don't build)

- "Open in chat / re-ask" actions wiring a hit back into a Claude Code prompt.
- Webview-based rich result browser with inline filtering and saved searches.
- A status-bar indicator with live index stats.
- Optional shared-index coordination with the CLI's scheduler (see §4.4).
- Semantic search via a local embedding model.
- Linux as a validated platform.

***

## 2. The central decision: how the extension gets results

### 2.1 The two candidate approaches

**Approach A — shell out to the existing Python CLI** (`python cc_history.py search --json`). Reuses every line of the existing, tested implementation. But it requires the user to have a working Python 3.9+ with an FTS5-enabled `sqlite3`, plus the script on disk, plus correct path discovery. That is an unacceptable hard dependency for a Marketplace extension whose value proposition is "install and search".

**Approach B — reimplement the indexer + searcher in TypeScript/Node**, self-contained, no Python. This is the Marketplace-correct path. The cost is re-implementing CLI §6 parsing and §5 schema in TS and solving "SQLite-with-FTS5 inside the VS Code extension host".

### 2.2 Decision

**Adopt Approach B as the only required path. Add Approach A as an optional, opportunistic fast path, not a dependency.**

Concretely:

- **B is the product.** The extension ships its own TS indexer and searcher and its own SQLite engine. With zero external tooling it can index and search. This is what gets published.
- **A is an optional accelerator.** A setting `ccHistory.useExistingCliIfPresent` (default `false`). When enabled, on activation the extension probes for a usable CLI install (see §2.5). If found, and if the **shared index** strategy (§4) is in effect, the extension can *defer indexing to the CLI's scheduler* and simply read the shared `index.db` — the extension then never runs its own indexer, saving CPU on machines where the CLI already keeps the index fresh. Search itself always runs in-process against the DB (the extension never shells out *per query* — see §2.4). A is purely about "who owns indexing", not "who answers queries".

This keeps the contract simple: **search is always in-process; indexing is in-process by default but can be delegated to the CLI scheduler when the user opts in and the CLI is present.**

### 2.3 Rationale

- A Marketplace extension must work for a user who has never heard of the Python CLI. Only B satisfies that.
- Re-implementing §6 parsing in TS is bounded work (the rules are small and already precisely specified) and is exactly the kind of logic that benefits from the shared test fixture (§8.4).
- Sharing the on-disk index format (not the code) gives the best of both: a user with the CLI installed gets a single index both tools keep warm; a user without it is fully served by the extension. The format is the contract, per CLI §5.

### 2.4 Why search is never a per-query shell-out (even in mode A)

Shelling out per keystroke/query has cold-start latency (Python interpreter spin-up of 50–300 ms), output-encoding hazards on Windows consoles (the CLI itself works around cp1252 in `_force_utf8_stdio`), and process-management complexity. Reading the DB in-process is faster, simpler, and identical across A/B. So the only thing A ever changes is whether the *extension's* background indexer runs.

### 2.5 CLI-presence probe (mode A)

When `useExistingCliIfPresent` is `true`, on activation run a single, time-boxed (2 s) detection:

1. Determine candidate index path (CLI default per its §4: `~/.cc-history/index.db` on macOS, `%LOCALAPPDATA%\cc-history\index.db` on Windows) — unless overridden by `ccHistory.indexPath`.
2. If that DB exists, opens cleanly, and carries the expected schema (a parseable `meta.last_run` row of the §4.3 shape), treat the CLI index as authoritative and skip the extension's own indexer; subscribe to file-watch on the DB to refresh results.
3. Otherwise fall back to B (own indexer, own/shared DB per §4).

**Recency does NOT gate authority.** Detection is "present and parseable", *not* "recently run". The extension cannot re-run the CLI, so a stale-but-valid CLI index is still the authoritative one when delegation is on; the extension simply watches the DB and lets the CLI's own scheduler catch up. (Treating staleness as a reason to take over would create two writers — exactly what §4.2 avoids.) If the user wants the extension to own freshness, they turn `useExistingCliIfPresent` off.

No attempt is made to *invoke* the CLI binary; presence is inferred from its index. This avoids PATH/shim discovery problems entirely and works whether the CLI was installed via shim or run as `python cc_history.py`.

***

## 3. SQLite-with-FTS5 in the extension host

The extension runs in VS Code's Node-based extension host (an Electron app). Reference versions **as of writing (2026-06)**: VS Code 1.98 shipped Electron 34 / Node 20.18; 1.97 shipped Electron 32 / Node 20.18. Treat these specific numbers as illustrative — re-check the Electron/Node pair of the `engines.vscode` floor (§7) at build time, since they drift with every VS Code line. The engine choice must survive VS Code's regular Electron bumps.

### 3.1 The three options, evaluated against FTS5 + Hebrew tokenizer + packaging

The CLI's schema **requires FTS5** with `tokenize='unicode61 remove_diacritics 2'` (CLI §5). Any engine that lacks FTS5 is disqualified outright.

| Option | FTS5 available? | Native build / ABI concern | Verdict |
| --- | --- | --- | --- |
| **`node:sqlite`** (Node built-in) | **No.** Node 22/23/24 compile their bundled SQLite *without* `SQLITE_ENABLE_FTS5`; `CREATE VIRTUAL TABLE ... USING fts5` fails with `no such module: fts5`. Also not present at all in Node 20 (VS Code's current runtime). | n/a | **Rejected.** No FTS5, and not even present in VS Code's Node 20. |
| **`sql.js`** (WASM) | **Only if specially built.** The official `sql.js` release bundles FTS3, *not* FTS5 — FTS5 requires a custom build with `-DSQLITE_ENABLE_FTS5`. Community fork `sql.js-fts5` exists but is unmaintained and lags upstream SQLite. | None (pure WASM, no ABI issues, no rebuild on Electron bumps). | **Fallback only.** Attractive for zero-native-build, but: must self-build/verify the FTS5 WASM, the whole DB is loaded into memory (a multi-hundred-MB transcript index becomes a memory problem), and writes mean re-serializing the DB. Acceptable for a small read-only index, risky for the indexer. |
| **`better-sqlite3`** | **Yes, by default.** Ships its own SQLite compiled with FTS5 (+FTS3/4, JSON1) enabled. Synchronous API, fast, exactly what an indexer wants. | **Yes — must be rebuilt against VS Code's Electron ABI.** This is the one real cost: `NODE_MODULE_VERSION` of Electron differs from stock Node, so a stock prebuilt binary throws an ABI mismatch in the extension host. | **Chosen.** The ABI problem is well-understood and solvable (§3.3). |

### 3.2 Decision

**Use `better-sqlite3` as the primary engine.** It is the only option that gives FTS5 + the required tokenizer out of the box, a synchronous on-disk API ideal for the incremental indexer, and good performance on large indexes without loading the DB into memory.

**Keep `sql.js` (FTS5 build) as a documented fallback** behind an internal `SqliteEngine` interface (§3.4), so that if a particular VS Code/Electron combination cannot load the native module, the extension degrades to **read-only search against an already-existing index** rather than failing to activate. v1 ships `better-sqlite3`; the `sql.js` adapter is a thin, optional second implementation (build it only if the native path proves fragile in the field — see §12 risk).

**The `sql.js` fallback is read-only-SEARCH only — it cannot index.** WASM `sql.js` holds the entire DB in memory and "persists" a write by re-serializing the whole database to a byte array. That is not a viable writer for an incremental indexer over a multi-hundred-MB index (whole-DB rewrite per commit, unbounded memory). The seam reflects this: the `sql.js` adapter's `openReadWrite` **throws**; only `openReadOnly` is implemented. Consequently the ABI-failure UX is precise: *"Search works against your existing index. Indexing is disabled until the native database engine loads — reopen the window or reinstall the matching extension build."* If no index exists yet when the native engine is unavailable, the empty-index state (§6.8c) explains that indexing needs the native engine.

### 3.3 Solving the `better-sqlite3` ABI/packaging problem

The native binary must match the Electron ABI of the *target* VS Code, across macOS (arm64 + x64) and Windows (x64). Strategy:

1. **Ship prebuilt binaries inside the `.vsix`, do not compile on the user's machine.** A Marketplace extension must not require a C toolchain on the user side.
2. **Build the binaries in CI with `@electron/rebuild`**, targeting the Electron version that matches the extension's `engines.vscode` floor. CI matrix: `{macos-arm64, macos-x64, windows-x64}`. (`@electron/rebuild` downloads the right Electron headers and compiles against them; this is the supported mechanism per Electron's native-modules docs.)
3. **Pin the Electron target explicitly** (an `electronVersion` in the build config) rather than letting it float, so a VS Code bump doesn't silently produce a mismatched binary. The pinned Electron must be `<=` the Electron in the *oldest* VS Code allowed by `engines.vscode`, and ABI-compatible with newer ones. In practice, target the Electron of the `engines.vscode` floor and re-verify on each VS Code minor.
4. **Per-platform `.vsix` (recommended) via `vsce --target`.** Publish platform-specific packages (`darwin-arm64`, `darwin-x64`, `win32-x64`) each containing only that platform's native binary. This keeps each download small and avoids shipping three binaries to every user. (Marketplace supports platform-specific extensions; the install client picks the right one.)
5. **Load guard:** wrap the `require('better-sqlite3')` in a try/catch. On ABI failure, log a clear actionable error, surface a notification ("cc-history: the database engine could not load for this VS Code build"), and fall back to the `sql.js` adapter if present. Never let activation throw.
6. **ABI drift test in CI:** load the prebuilt binary inside `@vscode/test-electron` (which runs the *real* VS Code Electron) and run a trivial `CREATE VIRTUAL TABLE ... USING fts5` + insert + match. If that test passes in CI against the pinned VS Code, the shipped binary is known-good for that VS Code line.

### 3.4 The `SqliteEngine` seam

Define a minimal interface so the engine is swappable and the rest of the code is engine-agnostic:

```ts
interface SqliteEngine {
  readonly canWrite: boolean;          // false for the sql.js fallback
  openReadWrite(dbPath: string): Db;   // indexer — sql.js adapter THROWS EngineReadOnlyError
  openReadOnly(dbPath: string): Db;    // searcher
}
interface Db {
  exec(sql: string): void;
  prepare(sql: string): Stmt;          // parameterized
  transaction<T>(fn: () => T): () => T; // for the per-file BEGIN IMMEDIATE pattern
  pragma(s: string): unknown;
  close(): void;
}
```

`better-sqlite3` maps onto this almost 1:1 (it already has `prepare`, `transaction`, `pragma`) and reports `canWrite === true`. The `sql.js` adapter implements `openReadOnly` over the WASM API and reports `canWrite === false`; its `openReadWrite` throws `EngineReadOnlyError` (it cannot host the incremental writer — see §3.2). The indexer guards on `engine.canWrite` before attempting a write pass and surfaces the §6.8 "indexing disabled" state when false. The indexer and searcher otherwise depend only on `SqliteEngine`/`Db`/`Stmt`.

***

## 4. Index ownership & on-disk location

### 4.1 Shared vs. own index

Decision: **default to a separate, extension-owned index, but make the path a setting and support pointing it at the CLI's index.**

- **Default (`ccHistory.indexPath` unset):** the extension uses its own DB at an extension-scoped path:
  - macOS: `~/.cc-history-vscode/index.db`
  - Windows: `%LOCALAPPDATA%\cc-history-vscode\index.db`

  Deliberately *not* the CLI's `~/.cc-history/index.db`, to avoid two independently-scheduled writers fighting over the same WAL and to avoid schema-drift surprises if the two projects' schemas ever diverge.
- **Opt-in sharing:** if the user sets `ccHistory.indexPath` to the CLI's `index.db` (or enables `useExistingCliIfPresent`, §2.5, which auto-discovers it), the extension reads/writes that shared DB. WAL mode (CLI §5.1) makes concurrent reader + single writer safe, so the extension reading while the CLI's scheduler writes is fine.

### 4.2 Why default to separate

- **Concurrency:** WAL allows many readers + one writer. Two writers (the CLI scheduler and the extension's own indexer) are serialized by SQLite's write lock and will mostly work, but it's needless contention and a footgun if both fire at once. Separate DBs sidestep it. When sharing is explicitly opted into, the user is also expected to let *one* side own indexing (the extension delegates to the CLI per §2.2/§2.5).
- **Schema drift:** the two codebases version independently. A separate default means an extension update can evolve its schema (e.g. add a column) without corrupting a CLI user's DB. (See §4.3.)
- **No-CLI case:** the common Marketplace user has no CLI; a separate, self-managed DB is the only thing that makes sense for them, so it's the right default to optimize for.

### 4.3 Schema compatibility & versioning

- The extension creates exactly the CLI §5 schema (same tables, columns, FTS5 config, triggers, PRAGMAs). The TS `SCHEMA` constant is a direct port of the Python `SCHEMA` (including `meta`).
- Store a schema marker in `meta`: `key='schema_version'`. On open, if the DB has a *newer* schema_version than the extension understands, open read-only and warn (don't migrate down). If older **and the DB is extension-owned**, run forward migrations. On a shared CLI DB lacking the marker, treat absence as "CLI baseline schema" (compatible).
- **Never run forward schema migrations on a shared / CLI-owned DB.** The CLI creates its tables with `CREATE TABLE IF NOT EXISTS` and has no awareness of columns the extension might add; if the extension altered a shared DB's schema, the CLI would keep writing the old shape into the new structure — a silent corruption vector. Therefore: migrations run **only** on the extension's own default DB. When the configured `indexPath` points at a non-default location (the user opted into sharing, §4.1) or `useExistingCliIfPresent` is on, the extension treats the DB as foreign: it may create the baseline schema if absent, but it **must not** `ALTER`/migrate it. A newer-than-understood shared DB is opened read-only with a warning; an older shared DB is used as-is (no migration) for whatever subset of columns both tools share.
- The extension writes its own `meta` `last_run` payload in the **exact same JSON shape** the CLI uses: `{at, entries, files, skipped, elapsed}` — note there is **no `errors` key** (the CLI tracks `errors` only in its log line and exit code, not in `last_run`). Matching this exactly keeps a shared DB's `stats` coherent across both tools. `at` is `_utc_now_iso()` (a `…Z` UTC timestamp); `elapsed` is seconds rounded to 3 decimals.
- **Content-affecting toggles break byte-equality regardless of schema version.** `indexThinking=false`, or any non-default `toolUseMaxBytes`/`toolResultMaxBytes`, produces an `entries` table the CLI would *not* reproduce from the same transcripts (missing thinking rows / different truncation points). This is independent of schema compatibility. If a user shares a DB with the CLI, they must leave these at CLI-default values, or accept that the shared index no longer round-trips byte-for-byte. The extension warns when a content-affecting toggle is non-default **and** the DB is shared/foreign.

### 4.4 Multi-window & cross-process write coordination (v1)

The extension-owned DB has one logical writer per OS user, but **N VS Code windows = N extension hosts = N candidate indexers** all pointed at the same default DB. The in-process single-flight guard (§5.1) is per-process and does **not** prevent two windows from indexing simultaneously; SQLite's `BEGIN IMMEDIATE` would serialize them, but they would still do redundant, contending work and double the file scans.

**v1 ships a cross-process advisory lock** (not deferred):

- A `meta` row `key='indexer_lock'`, value JSON `{owner, pid, host, acquiredAt, heartbeatAt}`. `owner` is a per-extension-host UUID generated at activation.
- Before a write pass, a window attempts to claim the lock inside a `BEGIN IMMEDIATE` transaction: it reads the row, and acquires only if the row is absent, owned by itself, or **stale** (heartbeat older than `3 × indexIntervalSeconds`, lower-bounded so a crashed holder is reclaimed within minutes). The claim write happens in the same transaction as the read so two windows cannot both win.
- The holder refreshes `heartbeatAt` periodically while indexing and on each per-file commit; it deletes/relinquishes the row when the pass completes or on clean shutdown (§5.4).
- Non-holders skip their own write passes and instead file-watch the DB to refresh open results (same mechanism as delegated mode A). This makes the lock the single coordination primitive for *both* multi-window and (future) CLI co-writing.
- The same `indexer_lock` convention is what a future CLI release could honor if both ever write concurrently; for v1 the CLI does not read it, so a shared-with-CLI DB still relies on the §2.2 "let one side own indexing" guidance. The lock fully solves the common case (multiple extension windows on the extension-owned DB).

### 4.5 Corrupt / unreadable DB recovery

Activation must **never throw** on a bad database. On opening either connection, catch SQLite open/read failures — specifically `SQLITE_CORRUPT`, `SQLITE_NOTADB`, and generic open failures (file present but unreadable, truncated, or not a SQLite file):

- Do not crash activation and do not silently swallow. Surface an actionable error notification: *"cc-history: the search index appears to be corrupt or unreadable."* with a single primary action **Rebuild Index** and a secondary **Show Log**.
- **Rebuild Index** on the extension-owned DB deletes the DB (and its `-wal`/`-shm` sidecars) and runs a full index pass from scratch. For a **shared/foreign** DB the extension must **not** delete it — instead it offers to switch to the extension-owned default path and rebuild there, leaving the CLI's DB untouched.
- A failed read-only open in search context degrades the same way: search reports the corrupt-DB state (not a bare error) and offers Rebuild.
- This path is exercised in tests by pointing `indexPath` at a deliberately-corrupt file and asserting activation succeeds + the recovery notification fires.

***

## 5. Indexing inside the extension lifecycle

Reuse the CLI's incremental model verbatim (CLI §6, §7.1): per-file `(path, mtime_ns, size)` skip check; resume from `last_offset`; one `BEGIN IMMEDIATE` transaction per file; abandon-and-retry on >10 malformed lines; FTS kept in sync by triggers. **The parsing rules are CLI §6 — implement them, do not invent new ones.** That means, in TS:

- Skip `queue-operation` and `summary` types.
- For `user`/`assistant`, read `message.content` blocks; map block types to `{role, kind, tool_name, text}` exactly per the CLI §6 table (`text`, `thinking`, `tool_use` → `name(stringify(input))` truncated to 4096 **bytes**, `tool_result` → stringified content truncated to 8192 **bytes**, `image` skipped).
- Robustness rules per CLI §6.1: stringified `message`, missing `timestamp` → file mtime, missing `sessionId` → filename stem, malformed-line counter, mid-read growth handled by next run.
- Block-handling rules that the conformance suite (§8.4) MUST exercise — these are subtle and easy to silently diverge on:
  - **Empty/whitespace `text` and `thinking` are DROPPED.** The CLI guards each with `if txt.strip():` — a block whose text is empty or only whitespace produces **no entry**. (A `text` block reads `block.text`; a `thinking` block reads `block.thinking`; both coerce `null`/missing to `""` first.) `thinking` is always emitted with role `assistant` regardless of the message role.
  - **`tool_result` with empty/whitespace stringified content is DROPPED, but `tool_use` is NOT — this asymmetry is intentional.** The CLI emits `tool_use` unconditionally (even `name()` with empty input survives), but applies `if text.strip():` *after* truncation to `tool_result`. Port both branches exactly.
  - **A bare string element inside a content array becomes a `text` entry** (role from the message), again only `if block.strip()`. A non-dict, non-string element is skipped.
  - **A `message` that is a JSON string** is `JSON.parse`d; if parsing fails, the raw string becomes a single `text` entry for the role (no `strip` guard on this fallback path — match the CLI).
- `stringify` must match the CLI's `_stringify`: a string is returned as-is; otherwise serialize with **Python `json.dumps(..., ensure_ascii=False)` separators** — i.e. `", "` between items and `": "` between key and value, and non-ASCII left unescaped. JS `JSON.stringify` defaults to **no spaces** after `,`/`:`, so the port must inject them (e.g. a custom serializer or post-process) to reproduce `Read({"file_path": "/a", "limit": 10})` byte-for-byte. Mismatched separators silently break shared-DB byte-equality for every `tool_use` row.
- **Truncation is byte-based UTF-8 with NO replacement character.** Port the CLI's `_truncate(text, limitBytes)` exactly:
  1. `const raw = Buffer.from(text, 'utf8')`. If `raw.length <= limit`, return `text` unchanged.
  2. Take `raw.subarray(0, limit)`. The CLI does `raw[:limit].decode("utf-8", errors="ignore")`, which **silently drops** a partial trailing multibyte sequence — it does **not** emit U+FFFD. Reproduce this: starting from index `limit`, back off over any trailing UTF-8 **continuation bytes** (`0x80–0xBF`), and if the byte immediately before the cut is an incomplete **lead byte** (`>= 0xC0`) whose full sequence does not fit within `limit`, drop it too — i.e. cut at the last position that ends a complete code point. Decode that prefix with `toString('utf8')` (which, given a clean code-point boundary, performs no substitution).
  3. Append the suffix `"\n[…truncated, full length " + raw.length + " bytes]"`, where `raw.length` is the **full original byte length** (`N`). Note the stored string therefore **exceeds** `limit` bytes (the suffix is added after the clip) — this matches the CLI and is intentional.
  - Use `Buffer` byte operations throughout, never JS string `.length` (UTF-16 code units). The shared fixture (§8.4) MUST include a case where the byte cut lands in the **middle of a Hebrew (2-byte UTF-8) character** so this boundary logic is actually tested.
- **Heads-up — CLI spec defect to fix:** the CLI spec §6 table currently says these limits are in "chars". That is wrong; the implementation (`cc_history.py:305`, `text.encode("utf-8")[:limit]`) truncates on **bytes**. The CLI spec must be corrected to say "bytes" (tracked as a CLI-spec erratum, not changed here). Everywhere this spec or the CLI spec says "chars" for truncation, read/fix it as "bytes".
- Project-name decoding per CLI §6.2 (see §11 for the TS port specifics).

### 5.1 When indexing runs

v1 has exactly **three** indexing triggers (activation, interval, explicit command). "Index before search" is deliberately **cut from v1** and deferred to a later phase — it is a latency optimization that adds coupling between the search path and the indexer, a second progress surface, and extra cancellation cases, for marginal freshness benefit over the interval + file-watch. Search in v1 always runs against the current index; a manual `Re-index` (or the interval) refreshes it.

- **On activation** (lazy — see §7 activation events): kick a single incremental index pass in the background. Never block activation on it. Gated by the cross-process lock (§4.4) — if another window holds it, this window watches instead.
- **On an interval:** a timer every `ccHistory.indexIntervalSeconds` (default 600, min 60, matching the CLI's clamp) triggers another incremental pass, only if one isn't already running in this process (single-flight; mirror the CLI's `MultipleInstancesPolicy=IgnoreNew`) **and** this window holds the cross-process lock (§4.4).
- **On command:** `cc-history: Re-index Transcripts` (incremental) and `cc-history: Rebuild Index` (full, equivalent to CLI `--full`). Explicit commands still respect the cross-process lock; if another window is mid-pass, the command waits briefly for the lock then reports "another window is indexing" rather than double-writing.
- **Delegated (mode A):** if the CLI index is authoritative (§2.5), none of the above writers run; instead a file watcher on the DB (and/or a periodic stat) refreshes any open results view.

### 5.2 Off the UI thread

The extension host is single-threaded JS; a multi-thousand-file index pass that parses JSON and writes SQLite would jank the host (and `better-sqlite3` is synchronous). Therefore **run the indexer in a Node `worker_thread`.**

- The worker owns the read-write `better-sqlite3` connection. The main thread never opens the DB read-write — this guarantees a single writer within the extension and keeps heavy CPU off the host thread.
- The native `better-sqlite3` binary must load inside the worker too; the worker `require`s it the same way (the ABI-correct binary works in both contexts).
- Main thread ↔ worker communication: a small message protocol (`{type:'index', mode:'incremental'|'full'}` → progress messages `{type:'progress', filesDone, filesTotal, entriesAdded}` → `{type:'done', summary}` / `{type:'error', message}`).
- The searcher uses a **separate read-only** connection (`openReadOnly`, SQLite URI `?mode=ro`, matching CLI §5.1). Reads run on the main thread in v1 (queries are bounded by `LIMIT`); move to the worker if a query ever proves slow. **Caveat on perf:** the query is an FTS `MATCH` join `ORDER BY ts DESC LIMIT N`. The `MATCH` itself is fast, but the `ORDER BY entries.ts DESC` over the FTS-join result is **not** covered by `idx_entries_project_ts` (which is `(project, ts)`, not `(ts)`), so a result set that the join makes large before the `LIMIT` can force a sort step. Do not assert "sub-ms" in docs; expect low-single-digit-to-tens-of-ms on a realistic index, and verify it against an exit criterion (§13 Phase 1) rather than assuming. If sort cost shows up on the author's real index, consider a covering index on `ts` (extension-owned DB only — never add an index to a shared CLI DB, §4.3).

### 5.3 Progress UI

- Background activation/interval passes: silent unless something is indexed; on completion of a pass that added entries, optionally update a status-bar item (`$(database) cc-history: N new`) that fades. No modal, no notification spam.
- Explicit `Re-index` / `Rebuild Index` commands: use `window.withProgress({location: ProgressLocation.Notification})` (or `Window` location for rebuild, which is long) showing files-done / files-total streamed from the worker, cancellable. Cancellation tells the worker to stop after the current file's transaction (never mid-transaction — preserve the crash-safety invariant from CLI §6.1).

### 5.4 Shutdown & interruption

- On `deactivate` (and on window reload/close), the extension **terminates the worker** (`worker.terminate()` after a short, bounded request to stop cleanly). An in-flight file is simply **not committed**: because each file is one `BEGIN IMMEDIATE` transaction and `last_offset` only advances on commit (CLI §6.1), an interrupted file leaves `last_offset` unchanged and the WAL consistent. The next run re-reads that file from its last committed offset — no corruption, no partial entry.
- The worker relinquishes the cross-process `indexer_lock` (§4.4) on clean stop; if it is hard-terminated, the lock's staleness/heartbeat rule reclaims it on the next run.
- `deactivate` must not block on a synchronous DB close that could hang. The main thread holds no read-write connection (the worker owns it, §5.2), so `deactivate` only needs to signal+terminate the worker and close the main thread's read-only connection (a fast, local close). Do not `await` an unbounded checkpoint in `deactivate`; rely on WAL durability instead.

***

## 6. UX surface

### 6.1 Commands (`contributes.commands`)

| Command id | Title | Notes |
| --- | --- | --- |
| `ccHistory.search` | `cc-history: Search Transcripts` | Primary entry. Opens the search Quick Pick. |
| `ccHistory.searchInProject` | `cc-history: Search Transcripts in Current Project` | Pre-fills the project filter from the active workspace folder's path (decoded to match an indexed project). |
| `ccHistory.reindex` | `cc-history: Re-index Transcripts` | Incremental pass. |
| `ccHistory.rebuildIndex` | `cc-history: Rebuild Index (Full)` | Drop + rebuild; confirmation prompt. |
| `ccHistory.showStats` | `cc-history: Show Index Stats` | Opens a read-only stats view (projects/sessions/entries-by-kind/bounds/db size/last run). |
| `ccHistory.openEntryContext` | `cc-history: Open Entry in Context` | Internal-ish; invoked from a result. |
| `ccHistory.copyEntryText` | `cc-history: Copy Entry Text` | Result context-menu action. |
| `ccHistory.revealTranscriptFile` | `cc-history: Reveal Transcript File` | Opens the source `.jsonl` at the hit's line in a normal editor. |

### 6.2 Search interaction — Quick Pick (primary) + TreeView (browsing)

**Decision: Quick Pick is the primary search surface; an Activity Bar TreeView is the secondary "results & filters" surface. No Webview in v1.**

Rationale: a Webview is the most flexible but the most expensive (CSP, message-passing, RTL handling, maintenance, security review) and over-engineered for a single-developer tool. Quick Pick gives instant, keyboard-first, fuzzy-feeling search that VS Code users already know; a TreeView gives a persistent place for results, grouping, and filter state. This matches VS Code's own Search/Find idioms.

**Quick Pick flow (`ccHistory.search`):**

1. A `QuickPick` opens with the prompt "Search Claude Code transcripts". Typing issues FTS queries (debounced ~150 ms). Query syntax mirrors CLI §7.2's `build_fts_query`: multi-word → auto-quoted single phrase by default; if the **operator regex** matches, the raw string passes through verbatim. The CLI regex is `["*():]|(?:\b(?:AND|OR|NOT|NEAR)\b)` — note it triggers passthrough on a **bare `:` anywhere** in the input (and on `"`, `*`, `(`, `)`, or the words AND/OR/NOT/NEAR). Port it exactly. Show a subtle hint in the placeholder.
   - **Transient-invalid-query handling (important for a per-keystroke Quick Pick):** because passthrough fires mid-typing, the user will frequently send FTS5 that is momentarily invalid (e.g. an unbalanced `(`, a dangling `NEAR`, a lone `:`). The searcher MUST `try/catch` the `SqliteError`/`SqliteError: fts5: syntax error` from `prepare`/`run` and, on a parse error, show a quiet placeholder/detail like *"keep typing…"* (and keep the previous results) rather than firing an error toast on every keystroke. Only a genuinely empty result set shows the §6.8 empty state. Never `throw` out of the debounced handler.
2. Each result item:
   - **label:** the snippet (FTS5 `snippet(...)` with `…` markers, one line, newlines collapsed) — RTL-handled per §6.6.
   - **description:** `kind · project(collapsed) · sessionPrefix`
   - **detail:** localized timestamp + role.
3. `QuickPickItemButtons` per item: "Open in context" (default on Enter), "Open source file", "Copy text".
4. Quick Pick **filter buttons** in the title bar (`QuickInputButtons` / title buttons): toggles/cyclers for `kind`, `role`, and a `project`/`date` sub-picker (see §6.5). Selected filters render as a title suffix (e.g. `Search — kind:thinking · 2w`).
5. Enter on an item runs `ccHistory.openEntryContext` for that entry.

**TreeView (`contributes.views` in a `cc-history` Activity Bar container):**

- A `TreeDataProvider` showing the **last search's results**, grouped (collapsible) by **project → session**, each leaf an entry (icon by kind, label = snippet, tooltip = full text + metadata). Re-running a search repopulates it.
- A second top-level node "Filters" exposing the active filters as editable items (click to change), so filter state is visible and persistent across the session.
- Inline item actions (`contributes.menus` `view/item/context`): open-in-context, open source file, copy text.
- This view is what makes results *browsable and persistent* (Quick Pick vanishes on focus loss).

### 6.3 "Show entry + context" — read-only virtual document

**Decision: a read-only virtual document via a `TextDocumentContentProvider`, not a Webview.**

- Register a `cchistory:` URI scheme. `ccHistory.openEntryContext(entryId)` opens `cchistory:/session/<sessionId>?focus=<entryId>&context=<N>` as a read-only doc.
- Content = the focal entry plus N entries before/after **in the same session, ordered chronologically** (exactly CLI §7.3 `show` semantics, default N=5, from `ccHistory.contextSize`). Each entry rendered as a Markdown-ish block: a header line (`▶` marker for the focal entry, `[localized ts] role/kind tool_name #id`) followed by the indented text.
- Open it with language `markdown` so VS Code renders headers/code fences nicely and the user can use the Markdown preview (which handles RTL correctly — see §6.6). The document is read-only (virtual scheme + provider returns content; no save).
- Provide a CodeLens or document link "Open source .jsonl at this line" using the entry's `file_path` + `line_no`.
- Why not Webview: the virtual document is free RTL/Markdown rendering, integrates with editor navigation, find-in-file, and copy, costs almost nothing, and needs no CSP. A Webview would be gold-plating here.

### 6.4 Results presentation summary

- Snippet width / markers mirror CLI §7.2 (`snippet(entries_fts, 0, '…','…','…', 16)`), normalized to a single line for Quick Pick labels and tree labels; full text shown in tooltips and the context document.
- Sort: most recent first (`ORDER BY ts DESC`), matching the CLI.
- Project path display collapses home: `~` on macOS, `%USERPROFILE%` on Windows (port CLI `collapse_home`).

### 6.5 Filters UI

Filters map exactly onto the CLI §7.2 search options:

- **kind:** `text | thinking | tool_use | tool_result` (multi-select toggle; default all).
- **role:** `user | assistant` (default both).
- **project:** a picker populated from `SELECT DISTINCT project FROM entries` (collapsed display), plus a "current workspace folder" shortcut. Maps to `project LIKE %…%`.
- **date:** `--since`/`--until` accepting the CLI's duration grammar (`1h/2d/1w/1m`) or an ISO date, offered as a small Quick Pick of presets (Last hour / Today / 7 days / 30 days / Custom…). Port `parse_duration_or_date`.
- **session:** optional, by prefix (offered from the context view's "more from this session" affordance rather than the main filter bar, to keep the bar small).

Filter state lives in the extension's in-memory session state and is reflected in both the Quick Pick title and the TreeView "Filters" node. It does **not** persist across VS Code restarts in v1 (keep it simple); `searchInProject` re-derives the project filter each time.

### 6.6 Hebrew / RTL handling

The author indexes and searches Hebrew. Rendering must be correct, not just non-crashing.

- **Tokenizer:** inherited for free — the DB uses `unicode61 remove_diacritics 2` (CLI §5), which tokenizes Hebrew sensibly. Nothing to do at query time beyond not mangling the query string.
- **Query input:** pass the user's raw query through unchanged (Unicode-safe); do not normalize away Hebrew. The phrase-quoting logic (`build_fts_query`) is byte/char-agnostic and works as-is.
- **Quick Pick / Tree labels:** VS Code renders item labels with the platform's bidi algorithm; mixed Hebrew+Latin+punctuation snippets can look scrambled. Mitigations: (a) wrap each rendered snippet with Unicode bidi isolates — prefix First-Strong Isolate `U+2068` (FSI) and suffix Pop Directional Isolate `U+2069` (PDI) — so each snippet is laid out independently and adjacent metadata isn't reordered; (b) keep metadata (kind/project/ts) in the `description`/`detail` fields, which are visually separated from the label, reducing bidi bleed.
- **Context document:** open as Markdown. VS Code's editor and Markdown preview both honor bidi; the user can also use the Markdown preview for fully correct RTL paragraph rendering. Do **not** inject HTML `dir="rtl"` wrappers (consistent with the author's documentation preference). Per-block isolation: wrap each rendered entry's text body in FSI/PDI as well so a Hebrew entry followed by a Latin one doesn't reorder headers.
- **No RTL CSS / no Webview** means no custom bidi CSS to maintain.

### 6.7 Opening, copying, keybindings

- **Open in context** (Enter / primary button) → §6.3 virtual doc.
- **Open source file** → open the `.jsonl` in a normal read-only-ish editor, reveal `line_no` (`TextEditorRevealType.InCenter`).
- **Copy entry text** → full (untruncated-as-stored) `text` to clipboard.
- **Keybindings (`contributes.keybindings`):** `ccHistory.search` → `ctrl+alt+h` (Win) / `cmd+alt+h` (mac), only `when` not in an input context to avoid clobbering. Keep keybindings minimal and non-conflicting; document them in the README rather than grabbing common chords.

### 6.8 Empty & degraded states

A bare "No results." is insufficient — the user can't tell "nothing matched" from "nothing indexed" from "no transcripts exist". Specify distinct UI for each:

- **(a) Transcripts root absent** (`~/.claude/projects` does not exist): the Quick Pick / tree show *"No Claude Code transcripts found at `<path>`. If Claude Code stores data elsewhere, set `ccHistory.claudeProjectsPath`."* with a button to open the setting. No indexing is attempted.
- **(b) Root present but empty** (exists, zero `*.jsonl`): *"No transcripts to index yet at `<path>`. Run some Claude Code sessions, then re-index."* with a **Re-index** action.
- **(c) Index empty after a successful first run** (root had files but the index has zero entries — e.g. everything was skipped/empty): *"Indexed `<path>` but found no searchable entries."* plus a **Show Index Stats** / **Show Log** action.
- **(d) Indexing disabled (native engine unavailable, §3.2)**: *"Search works against your existing index; indexing is disabled until the native database engine loads."* If there is also no existing index, combine with (a)/(b) messaging: *"…and no index exists yet, so search is unavailable on this build."*
- **(e) Query matched nothing** (the genuine no-results case): plain *"No matches for `<query>`."* — only this case uses the simple message, and only when the index is non-empty and the query parsed cleanly.

***

## 7. Extension manifest essentials (`package.json`)

- **`engines.vscode`:** **pin the floor at `^1.96.0`** (gives stable `QuickPick`, `TreeView`, worker support, platform-specific packaging). This floor is **load-bearing for the ABI**: it pins the Electron/Node target the native binary is compiled against (§3.3). The `@electron/rebuild` `electronVersion` must be the Electron of `1.96.0` (look it up at build time; treat the §3 reference numbers as illustrative, "as of writing"). Raising the floor later means re-pinning Electron and re-packaging; lowering it is not allowed without re-verifying the ABI against the older line. Re-verify the bundled binary against this floor's Electron on every VS Code minor bump.
- **`main`:** the esbuild-bundled `./dist/extension.js`.
- **`activationEvents` — avoid `*`. Lazy activation only:**
  - `onCommand:ccHistory.search` (and the other commands).
  - `onView:ccHistoryResults` (when the Activity Bar view is revealed).
  - **No** `onStartupFinished` by default — indexing-on-activation (§5.1) only matters once the user actually opens the feature; activating on first command keeps idle VS Code untouched. (If field feedback shows users want the index always-warm, add an opt-in `ccHistory.indexOnStartup` setting that, when true, contributes `onStartupFinished` behavior — but ship default-lazy.)
- **`contributes`:** `commands`, `configuration` (§9), `viewsContainers.activitybar` (a `cc-history` container with an icon), `views` (`ccHistoryResults` tree), `menus` (`view/item/context` + `commandPalette` gating — see below), `keybindings`.
- **Command Palette gating (`menus.commandPalette`):** the result-context-only commands `ccHistory.openEntryContext`, `ccHistory.copyEntryText`, and `ccHistory.revealTranscriptFile` take an entry argument and are meaningless when invoked cold from the palette. Gate each with `{ "command": "ccHistory.<id>", "when": "false" }` so they do **not** appear in the Command Palette; they are reachable only via Quick Pick item buttons and the tree `view/item/context` menu. The user-facing palette commands are `search`, `searchInProject`, `reindex`, `rebuildIndex`, `showStats`.
- **`capabilities`:**
  - `untrustedWorkspaces`: `{ "supported": true }` — the extension reads only the user's own `~/.claude` data and its own DB, never workspace files or workspace config that could be malicious; it executes no workspace code. So it is safe in untrusted workspaces. (Double-check: it must not read project-root config that could be attacker-controlled — it doesn't.)
  - `virtualWorkspaces`: `{ "supported": false, "description": "cc-history reads local transcript files and needs a native database engine, so it cannot run in a browser/virtual-filesystem-only workspace." }` — it requires a Node extension host and the `better-sqlite3` native module, neither of which exists in the web (browser) host. Note this is about **virtual/web workspaces**, which is *distinct* from Remote-SSH/WSL: Remote-SSH/WSL run a full Node host on the remote and ARE supported (see `extensionKind`).
- **`extensionKind` — the architecturally load-bearing decision (revised):** The data the extension reads (`~/.claude/projects`) lives **wherever Claude Code ran**. The author runs Claude Code both locally and inside **WSL / Remote-SSH**, where `~/.claude` is on the *remote/workspace* machine. Therefore the extension must be able to run **workspace-side** (in the remote extension host) so `os.homedir()` resolves to the machine that actually holds the transcripts and the native module loads on that host's platform.
  - **Set `"extensionKind": ["workspace", "ui"]`.** VS Code prefers the **first** entry, so this means: when a Remote-SSH/WSL window is open, run **workspace-side** (remote host) — correct, because the transcripts and `~/.claude` are there; the per-platform native binary must therefore also be built for the remote's platform/arch (Linux remotes are a v1 *gap*, consistent with the §1 Linux scope cut — Remote into a Linux host is not a validated v1 target). When there is no remote (a plain local window), `workspace` *is* the local host, so it runs locally as expected. The `ui` fallback covers hosts where a workspace install isn't available.
  - This is **not** the same as the old `["ui"]` recommendation, which was wrong: `ui`-only would force the extension to the *local* side even when Claude Code (and `~/.claude`) live on the remote — it would then index the wrong (local) home dir. The corrected reasoning: **run where the home dir / transcripts are**, which for Remote-SSH/WSL is workspace-side.
  - **Why this must be decided now even though Remote *testing* is Phase 3:** `extensionKind` and the resulting "which host loads the native binary / resolves `os.homedir()`" contract are **breaking to change after publish** (it moves which machine the extension runs on and which binary must ship). The path-resolution seam (§11) is written from v1 to never assume the local machine — it always derives from the host it runs on. Validated Remote test passes land in Phase 3; the architecture lands now.
- **What's bundled in the `.vsix`:** the bundled `dist/extension.js`, the bundled `dist/worker.js`, the target platform's `better-sqlite3` `.node` binary (per-target `.vsix`, §3.3 — and note the per-target set must include any Remote target platforms you intend to support, which is why Linux-remote is a known v1 gap), the extension icon, README, CHANGELOG, LICENSE. The optional `sql.js` WASM (if the fallback is shipped). Exclude `node_modules` source, tests, fixtures, maps (except a stripped prod sourcemap if desired).

### 7.1 Publishing identity — TODO (blocks publishing, not building)

These are required before `vsce publish` but do not block any build/test phase. Marked TODO for the author:

- **Publisher ID** — TODO (the author's Marketplace publisher; e.g. derived from the `or.benozio@gmail.com` account). Determines the full extension id `<publisher>.cc-history`.
- **Extension `name` / `displayName`** — TODO. Proposed `name: "cc-history"`, `displayName: "cc-history — Claude Code transcript search"`; confirm there's no Marketplace name clash.
- **Icon** — TODO: design a 128×128 PNG `icon.png`, or reuse the CLI project's mark if one exists.

Building, testing, and packaging an unsigned `.vsix` for local install do **not** require these; only Marketplace publish does.

***

## 8. Build & tooling

### 8.1 Language & layout

- **TypeScript**, `strict` mode. Target ES2021 / module CommonJS for the extension host (Node 20).
- Source: `src/extension.ts` (activation, commands, UI), `src/worker/indexer.worker.ts` (worker entry), `src/core/*` (engine seam, parser, search, project-decode, paths — the engine-agnostic core), `src/ui/*` (quickpick, tree, contextDoc).

### 8.2 Bundling

- **esbuild** (fast, simple, the de-facto VS Code choice) producing `dist/extension.js` and `dist/worker.js` as two entry points. Mark `better-sqlite3` as **external** (native module must be required at runtime from the packaged binary, not bundled). Mark `vscode` external (always). Bundle everything else.
- The worker is a separate esbuild entry so `new Worker(path.join(__dirname,'worker.js'))` resolves inside the packaged extension.

### 8.3 Native module handling in the build

- Dev: `npm i better-sqlite3` then `npx @electron/rebuild -v <electron-version-of-local-vscode>` (or use `@vscode/test-electron`'s downloaded Electron) so local F5 debugging works.
- CI/release: build the `.node` per target with `@electron/rebuild` against the **pinned** Electron, copy the resulting binary into a `prebuilds/<target>/` folder, and have `vsce package --target <target>` include only that target's binary. Produce one `.vsix` per target (§3.3).

### 8.4 Tests

- **Cross-implementation conformance via a language-neutral golden file (the data contract):** rather than have the TS suite depend on the Python repo's structure / `sys.path` / `test_indexer.py` internals (fragile, and it would couple the extension build to a shipped tool's layout), the contract is a checked-in **`expected-entries.json`** generated from `sample-session.jsonl`.
  - The **CLI's own test** emits `expected-entries.json`: it runs the Python parser over `sample-session.jsonl` and serializes, in source order, an array of `{role, kind, tool_name, text}` objects (the exact rows the indexer would insert, post-truncation). This file is committed alongside the fixture.
  - **Both** suites assert against it: the Python test asserts its parser reproduces the file (so it can't drift unnoticed), and the **TS parser test loads the same `expected-entries.json` and asserts byte-exact equality** of its produced rows. Neither suite imports the other's code. The file is the contract.
  - The fixture **MUST include**, and `expected-entries.json` therefore pins, these tricky cases (§5 parsing rules): (i) a `tool_use` whose `name(stringify(input))` exceeds 4096 bytes so truncation + the `[…truncated, full length N bytes]` suffix is exercised; (ii) **a `tool_use` or `tool_result` whose byte cut lands in the middle of a multibyte Hebrew character**, proving the byte-boundary back-off (no U+FFFD); (iii) an empty/whitespace `text` and an empty/whitespace `thinking` block (must produce no row); (iv) a `tool_result` with empty stringified content (dropped) **and** a `tool_use` with empty input (kept) — the asymmetry; (v) a bare string element inside a content array (becomes `text`); (vi) a `tool_use` whose JSON input has multiple keys, to pin the Python-`json.dumps` `", "`/`": "` separator output; (vii) Hebrew text for tokenizer/MATCH coverage.
- **Other unit tests (no VS Code needed)** for the engine-agnostic core, run under `node`/`vitest` or `mocha`:
  - **Project decode** — table-driven tests for the §6.2 examples (macOS `-Users-…`, Windows `c--Users-…`), `cwd` fast path, greedy resolution (mockable FS), naive fallback. Include the Windows `c--` → `c:\` case explicitly.
  - **Search query building** — phrase auto-quote vs. operator pass-through (port `build_fts_query` and the `["*():]|…AND|OR|NOT|NEAR` regex), the bare-`:` passthrough case, and `parse_duration_or_date`. **Do NOT enshrine the CLI's `parse_duration_or_date` date bug:** for a bare ISO date (no duration suffix) the CLI returns a **tz-naive local** timestamp (`strftime("%Y-%m-%dT%H:%M:%S")`, no `Z`), while the `ts` column it compares against is UTC `…Z` and the duration branch returns UTC. So `--since 2026-01-01` compares a local-naive string against UTC strings — an off-by-timezone bug. The TS port should **normalize ISO dates to UTC** (append `Z` / treat as UTC) so date filtering is correct, and a test should assert the corrected behavior (noting the divergence from the CLI here is intentional). Flag this back to the CLI as an erratum.
  - **Truncation** — direct unit tests of `_truncate` byte-boundary behavior independent of the golden file: ASCII at-limit, ASCII over-limit, multibyte char straddling the limit (Hebrew + an emoji/4-byte case), suffix byte-count `N` correctness.
  - **Schema** — create the schema in an in-memory/temp `better-sqlite3` DB, assert FTS5 table creates and a Hebrew `MATCH` returns the seeded row (proves the tokenizer works in the shipped engine).
- **Integration tests with `@vscode/test-electron`:**
  - Activation doesn't throw; native binary loads in the real VS Code Electron (the ABI guard test from §3.3.6).
  - End-to-end: point `ccHistory.indexPath` at a temp DB, index the fixture (copied into a temp `~/.claude/projects/<encoded>/…`), run `ccHistory.search`, assert results; open-in-context produces the expected virtual doc.
- **CI** runs unit tests on every push; integration tests on the matrix `{macos-latest, windows-latest}` (the two supported platforms).

***

## 9. Configuration (`contributes.configuration`)

All keys namespaced `ccHistory.*`:

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `ccHistory.claudeProjectsPath` | string | `""` (auto: `~/.claude/projects`) | Override the transcripts root (CLI §4 source-of-truth). |
| `ccHistory.indexPath` | string | `""` (auto: extension-owned path, §4.1) | Override DB location; set to the CLI's `index.db` to share. |
| `ccHistory.useExistingCliIfPresent` | boolean | `false` | If true, auto-discover the CLI index and delegate indexing (§2.5). |
| `ccHistory.indexOnStartup` | boolean | `false` | If true, warm the index on VS Code startup (adds `onStartupFinished`). Default off keeps idle VS Code untouched. |
| `ccHistory.indexIntervalSeconds` | number | `600` (min 60) | Background re-index interval; clamped to ≥60 like the CLI. |
| `ccHistory.indexThinking` | boolean | `true` | Index `thinking` blocks (CLI default true). When false, the indexer skips them. **Breaks shared-DB byte-equality** (§4.3). |
| `ccHistory.toolResultMaxBytes` | number | `8192` | Byte truncation for `tool_result` (CLI §13). |
| `ccHistory.toolUseMaxBytes` | number | `4096` | Byte truncation for `tool_use`. |
| `ccHistory.resultLimit` | number | `50` | Max results per search (extension default a bit higher than the CLI's 20 since the UI scrolls). |
| `ccHistory.contextSize` | number | `5` | Entries before/after in the context view (CLI `show --context`). |
| `ccHistory.snippetWidth` | number | `120` | Snippet width hint. |

Changing `indexThinking`/`toolResult*`/`toolUse*` affects **future** indexing only; surface a hint to run `Rebuild Index` for the change to apply retroactively (just like the CLI requires a `--full`). These are also **content-affecting toggles** that break shared-DB byte-equality (§4.3) — warn if set non-default while sharing a DB.

### 9.1 Numeric clamps

Enforce bounds in **two** places, defensively: declare `minimum`/`maximum` in the `configuration` JSON schema (so the Settings UI rejects bad input), **and** clamp again at read time in code (so a hand-edited `settings.json` can't inject an out-of-range value):

- `indexIntervalSeconds`: min 60 (CLI parity), max e.g. 86400; default 600.
- `resultLimit`: min 1, max e.g. 500; default 50.
- `contextSize`: min 0, max e.g. 100; default 5.
- `snippetWidth`: min 8, max e.g. 256; default 120 (FTS5 `snippet` token count is separately capped per CLI §7.2).
- `toolUseMaxBytes` / `toolResultMaxBytes`: min e.g. 256, max e.g. 1048576; defaults 4096 / 8192.

### 9.2 On-change behavior at runtime

Subscribe to `workspace.onDidChangeConfiguration` and react precisely (do not require a window reload for any of these):

- **`indexPath` or `claudeProjectsPath` changed:** this changes *which DB / which transcripts*. Stop any in-flight indexing, **close the current connections** (read-only searcher and, if held, signal the worker to close its writer), re-resolve paths, reopen against the new location, and trigger an incremental index pass (or, if the new DB doesn't exist, a full one). Clear the in-memory last-search results (they referenced the old index). Re-evaluate mode A detection (§2.5) since the index identity changed.
- **`useExistingCliIfPresent` changed:** re-run the §2.5 probe and switch between own-indexer and delegated mode accordingly.
- **`indexIntervalSeconds` changed:** reset the interval timer to the new (clamped) value.
- **`indexThinking` / `toolUseMaxBytes` / `toolResultMaxBytes` changed:** future-only (above); show the "run Rebuild Index to apply" hint; no automatic reopen.
- **`resultLimit` / `contextSize` / `snippetWidth` changed:** apply on the next search / next context open; no reindex.

***

## 10. Repo layout — separate repo + golden-file contract

**Decision: a separate `cc-history-vscode` repository, with the cross-tool guarantee carried by the golden file (§8.4), not by a shared directory tree.** The earlier "restructure the CLI into a monorepo" plan is **rejected as the default** because it is needlessly disruptive to an *already-published* tool: moving `cc_history.py` and `tests/fixtures/sample-session.jsonl` breaks the CLI's `tests/test_indexer.py` (`sys.path` import of the module and the hard-coded fixture path), its `install.sh`/`install.ps1`, and any user instructions or shims that reference the current paths. None of that risk buys anything the golden file doesn't already give.

The conformance link does **not** require co-location. The contract is the committed **`expected-entries.json`** (§8.4): the CLI repo generates it from its fixture; the extension repo vendors a **copy of the fixture + `expected-entries.json`** (checked in, with a recorded source commit / checksum so drift is auditable). Both suites assert against the same data. This is a *data contract*, not a repository-structure dependency — which is exactly what makes it robust.

Proposed extension repo `cc-history-vscode/`:

```
cc-history-vscode/
├── package.json                  # extension manifest
├── src/
│   ├── extension.ts
│   ├── core/                     # engine seam, parser, search, decode, paths, truncate
│   ├── worker/indexer.worker.ts
│   └── ui/                       # quickpick, tree, contextDoc
├── test/
│   ├── fixtures/
│   │   ├── sample-session.jsonl  # copied from the CLI repo (record source commit/sha)
│   │   └── expected-entries.json # the golden contract, copied from the CLI repo
│   └── …                         # unit + @vscode/test-electron
├── prebuilds/<target>/           # CI-produced native binaries (gitignored)
├── icon.png  CHANGELOG.md  README.md  LICENSE
└── .github/workflows/vscode.yml  # build/test/package
```

On the CLI side, the only required change is **additive and non-breaking**: have the CLI test suite emit `expected-entries.json` next to its existing fixture (no files move, no paths change). 

**Monorepo remains an option, but is explicitly non-load-bearing:** if the author later prefers one repo, the same golden-file contract works inside it unchanged — nothing in this spec depends on co-location. Do not restructure the published CLI solely to enable conformance.

***

## 11. Cross-platform concerns

- **Path resolution always derives from the host the extension runs on — never assume "local".** Because `extensionKind` is `["workspace","ui"]` (§7), in a Remote-SSH/WSL window the extension runs **on the remote**, so `os.homedir()` there returns the remote user's home and `~/.claude/projects` resolves to the remote machine's transcripts — which is exactly correct (that's where Claude Code ran). The path seam (a single `Paths` module) must therefore use `os.homedir()`, `os.platform()`, and `process.env` of the **running host** and must never special-case "the local machine" or read the local filesystem when running remotely. All filesystem access goes through Node `fs` on the running host (or the VS Code `workspace.fs` API where a `Uri` is appropriate), so it transparently targets the remote in Remote scenarios. This is the v1 architecture even though a validated Remote test pass is Phase 3.
- **Transcripts root:** `~/.claude/projects` on both platforms (CLI §4 — same on macOS and Windows). Resolve via `os.homedir()` **on the running host**; honor `ccHistory.claudeProjectsPath` override. (A remote `settings.json` can point it elsewhere on the remote.)
- **App-data / index path:** per §4.1 — `~/.cc-history-vscode/` (mac/Linux) and `%LOCALAPPDATA%\cc-history-vscode\` (Windows), resolved on the running host. Use Node `os` + `process.env.LOCALAPPDATA` with a `~/.cc-history-vscode` fallback (mirror the CLI's `Paths.app_data_dir` logic). In a Remote window the DB lives on the remote, alongside the remote transcripts — correct, since the native engine also runs there.
- **Path handling:** store `file_path` with platform-native separators **of the host that indexed it** (CLI §5 stores absolute native paths). Use `path` module throughout; never hand-concatenate separators. FTS and SQL don't care, but file-open and the `LIKE` project filter do. (A DB indexed on a Linux remote stores POSIX paths; opening it locally on Windows would mis-resolve those paths — another reason the index is host-local, §4.1.)
- **Project-name decoding (§6.2 of the CLI), TS port — port it faithfully, including:**
  - `cwd` fast path first (scan first ~8 lines for a `cwd` that is an existing dir).
  - Greedy FS-walk with **case-insensitive existence checks on Windows** (the CLI's `_path_exists_ci`; in Node, compare lowercased `fs.readdirSync(parent)` names since `fs.existsSync` is already case-insensitive on Windows NTFS but the *reconstruction order* still needs the dir listing for the `. _` ambiguity).
  - Windows drive-root special case: folder `c--Users-…` → root `c:\`, because both `:` and `\` collapsed to `-` (CLI §6.2 note — greedy can't recover `c:\` from the name alone, hence cwd-first is mandatory).
  - Naive fallback (`-`→`/` mac, `-`→`\` win, leading `<letter>--`→`<letter>:\`).
- **UTF-8 everywhere:** read transcript files as UTF-8 with replacement on bad bytes (CLI decodes `errors="replace"`); the extension host is already UTF-8, so no console-codepage workaround is needed (that was a CLI-only problem). Byte-based truncation must use `Buffer.byteLength`/`Buffer.slice` on UTF-8, not JS string `.length` (which is UTF-16 code units), to match the CLI exactly.
- **`better-sqlite3` per-OS binaries:** macOS arm64 + x64, Windows x64 (§3.3). Apple Silicon vs Intel must both be covered for the author and Marketplace users.

***

## 12. Risks & open questions

### Risks

- **🔴 Worker + native module loading from a PACKAGED `.vsix` — the top risk.** Loading `better-sqlite3` inside a `worker_thread` is a *distinct, known failure mode* from main-thread loading (worker module resolution, `__dirname`, and prebuilt-binary discovery all differ), and resolution from an **installed** `.vsix` differs again from the dev/F5 tree. A dev-machine success proves little. Phase 0 must validate the real path: `vsce package` → install the `.vsix` → load `better-sqlite3` from the **installed** extension path **inside a worker**, on **both macOS and Windows**. **Pre-decided fallback if the worker+native+packaged path is flaky:** run the native engine on the **main thread** with batched, periodically-yielding transactions (chunk files, `await` between batches to unblock the host) — or, second choice, a `child_process.fork`ed indexer process. Either keeps writes single-owner; the `SqliteEngine`/worker-protocol seam (§5.2) is written so the execution host (worker vs. main-thread-batched vs. forked child) is swappable without touching `core/`. Resolve this in Phase 0 before any UI work.
- **🔴 `better-sqlite3` ABI drift across VS Code/Electron bumps** — the single biggest *ongoing* maintenance cost. Mitigation: pinned Electron target (§7 floor), CI ABI test in real VS Code (§3.3.6), load-guard + `sql.js` **read-only** fallback so a mismatch degrades to search-against-existing-index instead of a dead extension. Re-package on VS Code minor bumps.
- **🟡 Multi-window contention** — addressed in v1 by the cross-process `indexer_lock` (§4.4); residual risk is lock-staleness tuning. Without the lock, N windows would redundantly scan and contend; with it, one window indexes and the rest watch.
- **🟡 Corrupt/unreadable DB** — addressed by the §4.5 recovery path (activation never throws; one-click Rebuild). Residual risk is a corrupt *shared* CLI DB the extension must not delete.
- **🟡 Shared-DB write contention** — only a risk if a user shares the CLI's DB *and* lets both indexers write. Mitigated by §2.2 (delegate indexing to one owner), §4.2 (separate default), and the §4.3 migration prohibition. Document the "let one side index" guidance.
- **🟡 `sql.js` fallback is search-only on an existing index** (§3.2) — it cannot index. If a user only ever has the fallback engine and no pre-existing index, they get no search until the native engine loads. Acceptable degradation, surfaced via §6.8d.
- **🟡 Remote-SSH/WSL native binary coverage** — the per-target binary set must cover remote platforms; a **Linux remote is a v1 gap** (no Linux binary shipped, consistent with §1). macOS/Windows remotes are covered by the same binaries as local.
- **🟢 Hebrew bidi rendering** — handled by FSI/PDI isolation + Markdown context view (§6.6); residual risk is purely cosmetic.
- **🟢 Snippet/parse divergence from the CLI** — caught by the golden-file conformance contract (§8.4).
- **🟢 FTS sort cost** — `ORDER BY ts DESC` over the FTS join isn't index-covered (§5.2); verified by a Phase-1 timing exit criterion, fixable with a covering index on the extension-owned DB.

### Open questions for the author

(Resolved by the author's decisions: Remote/WSL support — **yes**, architecture is workspace-side from v1; byte-for-byte CLI parity — **yes**, a hard requirement, see §5/§8.4; index sharing default — **separate default, opt-in sharing**, §4.1; `engines.vscode` floor — **`^1.96.0`**, §7; repo — **separate repo + golden-file contract**, §10.)

Remaining open items:

1. **Publisher identity & Marketplace name / icon** — §7.1 TODO. Publisher ID, `displayName`, and a 128×128 `icon.png`. Blocks `vsce publish` only, not building.
2. **Bundle the `sql.js` fallback in v1, or ship `better-sqlite3`-only and add it reactively** if ABI issues surface? Recommendation: ship native-only for v1 simplicity, keep the `SqliteEngine` seam so the read-only fallback (§3.2) is a later drop-in.
3. **Keybinding chord:** is `ctrl+alt+h` / `cmd+alt+h` acceptable, or leave the search keybinding unset and rely on the Command Palette?

***

## 13. Phased roadmap

Each phase has a concrete, testable exit criterion. Earlier phases never depend on later ones. Phase 0 validates the riskiest assumption (native SQLite+FTS5 in the real extension host) as cheaply as possible.

### Phase 0 — Riskiest-assumption spike: native FTS5 in a worker, from an INSTALLED `.vsix`

The whole design hinges on `better-sqlite3` (with FTS5 + the Hebrew tokenizer) loading and querying inside VS Code's Electron on both macOS and Windows, **inside a `worker_thread`, from a packaged-and-installed extension** — not just from the dev tree (§12 top risk).

- Build a throwaway extension that, on a command, in a `worker_thread`, opens a temp `better-sqlite3` DB, creates the CLI §5 FTS5 schema, inserts one English and one Hebrew row, runs a `MATCH` for the Hebrew word, and shows the hit.
- Rebuild the native binary with `@electron/rebuild` against the `engines.vscode`-floor Electron, then **`vsce package` and *install the `.vsix`***, and run the worker test from the **installed** extension path (not F5) — on both macOS and Windows. Also prove it via `@vscode/test-electron` in CI on `macos-latest` + `windows-latest`.

**Exit criterion:** the Hebrew `MATCH` returns the seeded row, in a worker, inside real VS Code, **loaded from the installed `.vsix`**, on both OSes, from a CI-built binary. **If the worker+native+packaged path is flaky, adopt the pre-decided fallback (§12): native on the main thread with batched/yielding transactions, or a forked indexer** — and confirm *that* path with the same packaged-vsix test before proceeding. Do not start UI work until one execution model passes from an installed `.vsix`.

### Phase 1 — Core port + golden-file conformance (no UI)

- Port CLI §5 schema, §6 parser (incl. the exact drop/asymmetry rules and Python-`json.dumps` separators, §5), the byte-based `_truncate` (§5), §6.2 decode, search query building (incl. the operator regex), and the incremental indexer onto the `SqliteEngine` seam, all in `core/`.
- Stand up the incremental indexer (in whichever execution model Phase 0 validated) writing the extension-owned DB, with the cross-process `indexer_lock` (§4.4).
- Wire the **golden-file conformance contract** (§8.4): load the committed `expected-entries.json` and assert byte-exact TS parser output, including the multibyte/Hebrew-truncation case, the empty-drop cases, and the tool_use/tool_result asymmetry; Hebrew `MATCH` works; project-decode table tests pass (incl. Windows `c--`); `parse_duration_or_date` uses the **corrected** UTC date handling.

**Exit criterion:** `npm test` green (golden-file byte-exact); indexing the author's real `~/.claude/projects` produces an index whose `stats` match the CLI's `cc-history stats` on the same machine. **Plus a timing check:** an FTS `MATCH … ORDER BY ts DESC LIMIT 50` over the *real* index returns within an agreed budget (e.g. < 50 ms warm); if not, add the covering index (§5.2) before Phase 2.

### Phase 2 — Marketplace v1 (the publishable product)

- Quick Pick search (§6.2, incl. transient-invalid-FTS handling) with filters (§6.5), TreeView results (§6.2), read-only context virtual document (§6.3), open-source-file + copy actions, RTL handling (§6.6), and the distinct empty/degraded states (§6.8).
- Indexing triggers cut to **activation + interval + explicit command** (§5.1 — "index before search" is NOT in v1), with progress (§5.3), all lazy-activated (§7), plus the cross-process `indexer_lock` (§4.4), shutdown handling (§5.4), and corrupt-DB recovery (§4.5).
- Full `contributes.configuration` (§9) with two-place clamps (§9.1) and on-change behavior (§9.2), manifest capabilities, `extensionKind: ["workspace","ui"]` (§7), command-palette gating (§7), keybinding.
- Packaging: per-target `.vsix` with CI-built native binaries (§3.3, §8.3) — macOS arm64/x64 + Windows x64 (no `win32-arm64`), icon, README/CHANGELOG, ABI + golden-file tests in CI. `vsce publish` under the chosen publisher (§7.1 TODO).

**Exit criterion:** a fresh machine with **no Python and no CLI** installs the Marketplace extension, runs `cc-history: Search Transcripts`, gets a correct hit (including a Hebrew query), opens it in context, and the index stays fresh on the interval — verified on both macOS (arm64) and Windows (x64). Two windows open at once do not double-index (lock holds). A deliberately-corrupt index triggers the Rebuild recovery, not a crash. Sanity tests analogous to CLI §11 (1–5, 9) pass through the UI.

### Phase 3 — Hardening, opt-in CLI interop & Remote validation

- Mode A: CLI-index auto-discovery and indexing delegation (§2.5, present-and-parseable detection), DB file-watch refresh.
- Shared-index path support + schema-version guard + migration prohibition on shared DBs (§4.3).
- **Remote-SSH / WSL validated test pass:** confirm the v1 `extensionKind`/path architecture (§7, §11) actually resolves the remote `~/.claude` and loads the native binary on a supported remote (macOS/Windows remote; Linux-remote is the known gap). The architecture is already in v1; this phase *validates* it.
- Optional `indexOnStartup`, status-bar stats item.
- Robustness: ABI-failure fallback to the read-only `sql.js` engine (if shipped), cancellation correctness, large-index timing.

**Exit criterion:** with the CLI installed and `useExistingCliIfPresent` on, the extension reads the CLI's warm index, runs no redundant indexer, and refreshes results when the CLI's scheduler updates the DB — with no two-writer contention. A Remote-SSH/WSL window searches the remote's transcripts correctly.

### Phase 4 — v2 stretch (not scheduled)

Saved searches, re-ask-into-chat, Webview rich browser, semantic search, Linux validation. Per §1 v2 list.

***

## Appendix A — Cross-implementation conformance checklist (extension vs. CLI)

Byte-for-byte CLI parity is a **hard requirement** (the extension and CLI must be able to share `index.db`). The extension is "compatible" iff, for the same input and CLI-default content settings, it produces an index the CLI would also produce. The mechanical gate is the **golden file** `expected-entries.json` (§8.4) asserted byte-exact by both suites; this checklist is the human-readable contract behind it:

1. Same tables/columns/indexes/FTS5 config/triggers/PRAGMAs (CLI §5) — assert via schema diff. **No `ALTER`/migration on a shared DB** (§4.3).
2. Same `entries` rows for `sample-session.jsonl`: identical `(role, kind, tool_name, text)` in source order, including:
   - **Byte-based** (not char-based) UTF-8 truncation of `tool_use` (4096 B) / `tool_result` (8192 B), with the partial trailing multibyte sequence **dropped** (no U+FFFD), then the suffix `\n[…truncated, full length N bytes]` appended after the clip (stored length therefore exceeds the limit). The fixture pins a Hebrew-mid-character cut.
   - `tool_use` text built as `name(stringify(input))` with **Python-`json.dumps(ensure_ascii=False)` separators** (`", "` / `": "`), not JS-default no-space separators.
   - Empty/whitespace `text` and `thinking` blocks **dropped**; empty/whitespace `tool_result` **dropped** but empty-input `tool_use` **kept** (asymmetry); bare string array elements → `text`.
3. Same skip rules (`queue-operation`, `summary`, `image`) and robustness fallbacks (CLI §6.1).
4. Same project decoding outputs for the §6.2 examples on each platform.
5. Hebrew `MATCH` returns expected rows (tokenizer parity).
6. `meta.last_run` JSON shape **exactly** `{at, entries, files, skipped, elapsed}` — **no `errors` key** — so a shared DB's stats are coherent across tools.
7. Content-affecting toggles at CLI defaults: `indexThinking=true`, `toolUseMaxBytes=4096`, `toolResultMaxBytes=8192`. Any other value voids byte-equality (§4.3) regardless of schema version.
8. Note the deliberate, *non-parity* correction: the TS `parse_duration_or_date` fixes the CLI's tz-naive-ISO-date bug (§8.4); this affects only query-time filtering, not stored rows, so it does not break shared-DB byte-equality.
