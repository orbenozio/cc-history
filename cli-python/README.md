# cc-history

Local full-text search over your Claude Code session transcripts. It indexes every conversation on your machine into a SQLite+FTS5 database and lets you search across all of them — filtered by project, date, role, and content type — with a platform-native background job keeping the index fresh.

Python stdlib only, single file, cross-platform (macOS + Windows). Nothing leaves your machine.

## Install

**macOS / Linux**

```bash
git clone https://github.com/<you>/cc-history.git ~/Projects/cc-history
cd ~/Projects/cc-history
./install.sh
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/<you>/cc-history.git $HOME\Projects\cc-history
cd $HOME\Projects\cc-history
.\install.ps1
```

`install` initializes the database, runs a full index once, and registers a background job (macOS LaunchAgent / Windows Task Scheduler) that re-indexes every 10 minutes. Requires Python 3.9+ with FTS5 (bundled with standard CPython builds).

## Usage

Once installed, the commands are identical on both platforms:

```bash
# Search (multi-word queries match as a phrase by default)
cc-history search "auth flow" --limit 5

# Filter by time, kind, project, role, or session
cc-history search render --kind thinking --since 2w
cc-history search "TodoWrite" --project Diburit --role assistant

# Hebrew / non-Latin scripts work (FTS tokenizer handles diacritics)
cc-history search "שלום"

# Show a hit with surrounding conversation context
cc-history show 922 --context 3

# Machine-readable output
cc-history search foo --json

# Re-index on demand (incremental; --full rebuilds from scratch)
cc-history index

# Index health and scheduler status
cc-history stats
```

Sample output:

```
[2026-05-15 14:23:07] thinking | ~/Projects/foo · 9620f8ab #58611
  …the heatmap component needs to re-render when the filter changes, which means…
  → cc-history show 58611
```

**Query syntax:** multi-word input is auto-quoted as a single phrase. If your query contains an FTS5 operator (`"`, `*`, `(`, `)`, `:`, `AND`, `OR`, `NOT`, `NEAR`) it's passed through verbatim, so `auth OR login`, `"exact phrase"`, and `data*` all work. See `cc-history search --help`.

## How it works

`cc-history` walks `~/.claude/projects/**/*.jsonl`, parses each transcript line (user/assistant turns, thinking, tool calls, tool results), and stores them in a SQLite FTS5 index. Indexing is incremental — it resumes from the last byte offset per file, so re-runs are cheap. The full design is in [cc-history-spec.md](cc-history-spec.md).

## Uninstall

```bash
cc-history uninstall
```

This removes the background scheduler but **leaves your index intact**. To delete the data too:

- macOS / Linux: `rm -rf ~/.cc-history`
- Windows: `Remove-Item -Recurse -Force $env:LOCALAPPDATA\cc-history`

## Privacy

Everything stays local. The index lives at `~/.cc-history/index.db` (macOS/Linux) or `%LOCALAPPDATA%\cc-history\index.db` (Windows). No cloud, no telemetry, no network calls — nothing leaves your machine.

## License

MIT — see [LICENSE](LICENSE).
