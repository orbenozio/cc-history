// Phase 0 spike extension entry point.
//
// Registers one command that runs the FTS5 probe inside a worker_thread and
// reports the result. Search/index/UI are out of scope — this only de-risks the
// "native better-sqlite3 + FTS5 + Hebrew, in a worker, from an installed .vsix"
// assumption that the whole product depends on.

import * as vscode from "vscode";
import * as path from "path";
import { Worker } from "worker_threads";
import type { FtsProbeResult } from "./ftsWorker";

export function runProbeInWorker(workerPath: string): Promise<FtsProbeResult> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(workerPath);
    let settled = false;
    const done = (fn: () => void) => {
      if (settled) {
        return;
      }
      settled = true;
      worker.terminate();
      fn();
    };
    worker.once("message", (msg: FtsProbeResult) => done(() => resolve(msg)));
    worker.once("error", (err) => done(() => reject(err)));
    worker.once("exit", (code) => {
      if (!settled) {
        done(() => reject(new Error(`worker exited early with code ${code}`)));
      }
    });
  });
}

export function activate(context: vscode.ExtensionContext): void {
  const cmd = vscode.commands.registerCommand(
    "ccHistorySpike.runFtsTest",
    async () => {
      const workerPath = path.join(context.extensionPath, "out", "ftsWorker.js");
      try {
        const r = await runProbeInWorker(workerPath);
        const summary =
          `ok=${r.ok} | hits=${r.hebrewMatchHits} | folds niqqud=${r.diacriticsFold} | ` +
          `sqlite=${r.sqliteVersion} | ABI=${r.abiModules} | electron=${r.electron}`;
        if (r.ok) {
          await vscode.window.showInformationMessage(
            `✅ Hebrew FTS5 MATCH in worker: "${r.matchedText}"  (${summary})`
          );
        } else {
          await vscode.window.showErrorMessage(
            `❌ Probe failed. ${r.error ?? summary}`
          );
        }
      } catch (err) {
        await vscode.window.showErrorMessage(
          `❌ Worker/native load failed: ${err instanceof Error ? err.message : String(err)}`
        );
      }
    }
  );
  context.subscriptions.push(cmd);
}

export function deactivate(): void {
  /* nothing to clean up in the spike */
}
