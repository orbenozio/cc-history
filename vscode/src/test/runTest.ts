// Downloads VS Code (pinned to the version whose Electron ABI we rebuilt
// better-sqlite3 against) and runs the probe suite inside its real extension
// host. This is the CI-grade Phase 0 proof.

import * as path from "path";
import { runTests } from "@vscode/test-electron";

async function main(): Promise<void> {
  try {
    const extensionDevelopmentPath = path.resolve(__dirname, "../../");
    const extensionTestsPath = path.resolve(__dirname, "./suite/index");
    await runTests({
      version: "1.120.0", // Electron 39.8.8 / ABI 140 — matches `npm run rebuild`
      extensionDevelopmentPath,
      extensionTestsPath,
      launchArgs: ["--disable-extensions"],
    });
  } catch (err) {
    console.error("Phase 0 test run failed:", err);
    process.exit(1);
  }
}

void main();
