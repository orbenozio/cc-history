// Port of cc_history.py §6.2 project-name decoding. Folder names under
// ~/.claude/projects are dash-encoded paths; recover the real path via the
// cwd fast-path, then a greedy filesystem walk, then a naive fallback.
//
// Ground truth: ../../cc-history/cc_history.py — _decode_naive, _decode_greedy,
// resolve_project, _path_exists_ci.

import * as fs from "fs";
import * as path from "path";

const isWin = process.platform === "win32";

/** Case-insensitive existence check (Windows FS is case-insensitive). */
export function pathExistsCi(p: string): boolean {
  try {
    if (fs.existsSync(p)) {
      return true;
    }
  } catch {
    /* fall through */
  }
  if (!isWin) {
    return false;
  }
  const parent = path.dirname(p);
  const target = path.basename(p).toLowerCase();
  try {
    return fs.readdirSync(parent).some((c) => c.toLowerCase() === target);
  } catch {
    return false;
  }
}

/** Port of _decode_naive. */
export function decodeNaive(folderName: string): string {
  if (isWin) {
    const m = /^([A-Za-z])--(.*)$/.exec(folderName);
    if (m) {
      const rest = m[2].replace(/-/g, "\\");
      return `${m[1]}:\\${rest}`;
    }
    return folderName.replace(/-/g, "\\");
  }
  return folderName.replace(/-/g, "/");
}

/** Port of _decode_greedy — reconstruct against the live filesystem. */
export function decodeGreedy(folderName: string): string {
  const parts = folderName.split("-");
  const nonEmpty = parts.filter((p) => p.length > 0);
  if (nonEmpty.length === 0) {
    return folderName;
  }

  let current: string;
  let remaining: string[];

  if (isWin) {
    const drive = nonEmpty[0];
    current = `${drive}:\\`;
    remaining = nonEmpty.slice(1);
  } else {
    current = "/" + nonEmpty[0];
    if (!pathExistsCi(current)) {
      return folderName;
    }
    remaining = nonEmpty.slice(1);
  }

  for (const part of remaining) {
    const base = path.basename(current);
    const parent = path.dirname(current);
    const candidates = [
      path.join(current, part),
      path.join(parent, `${base}.${part}`),
      path.join(parent, `${base}_${part}`),
    ];
    const hit = candidates.find((c) => pathExistsCi(c));
    current = hit ?? path.join(current, part);
  }
  return current;
}

/**
 * Port of resolve_project: prefer a usable cwd hint, then greedy FS resolution,
 * then the naive fallback.
 */
export function resolveProject(folderName: string, cwdHint: string | null): string {
  if (cwdHint) {
    try {
      if (fs.statSync(cwdHint).isDirectory()) {
        return cwdHint;
      }
    } catch {
      /* not a usable dir */
    }
  }
  const greedy = decodeGreedy(folderName);
  if (pathExistsCi(greedy)) {
    return greedy;
  }
  return decodeNaive(folderName);
}
