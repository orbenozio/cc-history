# cc-history-vscode — Phase 0 spike

A throwaway spike that de-risks the single riskiest assumption behind the planned
**cc-history VS Code extension** (see `../cc-history/cc-history-vscode-spec.md`, §12 / Phase 0):

> native **`better-sqlite3`** (with **FTS5** + the `unicode61 remove_diacritics 2`
> tokenizer for Hebrew) loads and queries **inside a `worker_thread`**, **inside
> VS Code's Electron**, **from a packaged-and-installed `.vsix`**, on macOS and Windows.

If this fails, the whole architecture changes (fall back to main-thread batched
indexing or a forked process). So nothing else gets built until this passes.

## What it does

One command — **“cc-history Spike: Run FTS5 Hebrew Worker Test”** — spawns a
worker that:
1. loads the native `better-sqlite3`,
2. builds the CLI §5 FTS5 schema,
3. inserts an English row and Hebrew rows (one with niqqud),
4. runs `MATCH 'שלום'` and reports the hit + ABI diagnostics.

## Build & verify

Target ABI for this machine: **VS Code 1.120.0 / Electron 39.8.8 / Node 22.22.1 / modules 140.**

```powershell
npm install
npm run rebuild      # electron-rebuild better-sqlite3 against Electron 39.8.8
npm run compile      # tsc -> out/
npm test             # downloads VS Code 1.120.0 and runs the probe in its real Electron
```

`npm test` is the CI-grade proof (it runs inside the downloaded VS Code's
extension host). To verify the **installed-.vsix** path manually:

```powershell
npm run package                       # -> cc-history-fts-spike-0.0.1.vsix
code --install-extension .\cc-history-fts-spike-0.0.1.vsix
# reload, then run the command from the Command Palette; expect a ✅ toast.
```

## macOS

Same steps. `npm run rebuild` must run on the Mac too (native binaries are
per-platform); the `.vsix` is platform-specific. CI builds both via
`macos-latest` + `windows-latest`.

## Status

This is intentionally disposable. Once Phase 0 passes on both OSes, its findings
fold into the real extension and this repo can be archived.
