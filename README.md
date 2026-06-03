# claude-code-history

Local full-text search over your Claude Code conversation history. It indexes
Claude Code's local JSONL transcripts (`~/.claude/projects/**/*.jsonl`) into a
SQLite + FTS5 database and lets you search across every conversation — filtered
by project, date, role, and content type. Everything stays on your machine.

One product, multiple surfaces:

| Path | What | Status |
|------|------|--------|
| [`cli-python/`](cli-python/) | The original CLI (Python, stdlib-only, zero install). | ✅ shipping |
| [`vscode/`](vscode/) | VS Code extension. `src/core/` is the TypeScript engine. | 🟡 core + indexer done (Phase 1); UI is Phase 2 |
| [`shared/fixtures/`](shared/fixtures/) | Language-neutral conformance contract (`sample-session.jsonl` + `expected-entries.json`) both implementations assert against. | ✅ |

## Direction: one TypeScript core, thin frontends

The engine currently exists twice — Python (in `cli-python/`) and TypeScript (in
`vscode/src/core/`) — kept byte-for-byte identical via the `shared/fixtures/`
golden contract. The plan is to **converge on a single TypeScript core**
(`packages/core`) consumed by thin frontends:

```
packages/core/      ← the one engine (parser, indexer, FTS, project-decode, query)
apps/
  ├── cli/          ← Node CLI (replaces cli-python/)
  ├── vscode/       ← VS Code extension
  └── desktop/      ← (future) desktop app
```

Until the TypeScript CLI reaches parity, `cli-python/` stays as the working CLI
and is the only one that needs no Node runtime. The `shared/fixtures/` contract
keeps the two engines honest during the transition.

## Specs

- [`claude-code-history-spec.md`](claude-code-history-spec.md) — the CLI spec (data model, JSONL parsing, project-name decoding, scheduler).
- [`claude-code-history-vscode-spec.md`](claude-code-history-vscode-spec.md) — the VS Code extension spec (architecture, SQLite engine, indexing lifecycle, UX, roadmap).

## License

MIT — see [LICENSE](LICENSE).
