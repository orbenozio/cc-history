// Golden-file conformance: the TS parser/truncation must reproduce the CLI's
// output byte-exact, so the two tools can share one index.db.

import * as fs from "fs";
import * as path from "path";
import * as assert from "assert";
import { parseLine, truncate, ParsedEntry } from "../../core/parser";

interface GoldenEntry {
  role: string;
  kind: string;
  tool_name: string | null;
  text: string;
}
interface TruncationCase {
  name: string;
  build: { literal?: string; prefix?: string; char?: string; count?: number };
  limit: number;
  expected: string;
}
interface Golden {
  fixture: string;
  entries: GoldenEntry[];
  truncation: TruncationCase[];
}

function fixturesDir(): string {
  // vscode/out/test/suite -> monorepo root -> shared/fixtures
  return path.resolve(__dirname, "../../../../shared/fixtures");
}

export function testConformance(): void {
  const dir = fixturesDir();
  const golden: Golden = JSON.parse(
    fs.readFileSync(path.join(dir, "expected-entries.json"), "utf8")
  );
  const fixture = fs.readFileSync(path.join(dir, golden.fixture), "utf8");

  // 1. Parser parity over every fixture line.
  const got: ParsedEntry[] = [];
  for (const line of fixture.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) {
      continue;
    }
    const obj = JSON.parse(trimmed);
    got.push(...parseLine(obj).entries);
  }
  assert.strictEqual(
    got.length,
    golden.entries.length,
    `entry count: got ${got.length}, golden ${golden.entries.length}`
  );
  got.forEach((e, i) => {
    assert.deepStrictEqual(
      { role: e.role, kind: e.kind, tool_name: e.tool_name, text: e.text },
      golden.entries[i],
      `entry ${i} (kind=${e.kind}) diverges from golden`
    );
  });

  // 2. Byte-exact truncation, incl. the Hebrew mid-character boundary.
  for (const c of golden.truncation) {
    const input =
      c.build.literal !== undefined
        ? c.build.literal
        : (c.build.prefix ?? "") + (c.build.char ?? "").repeat(c.build.count ?? 0);
    const out = truncate(input, c.limit);
    assert.strictEqual(out, c.expected, `truncation case "${c.name}" diverges`);
  }

  console.log(
    `[conformance] PASS — ${got.length} entries byte-exact + ` +
      `${golden.truncation.length} truncation cases`
  );
}
