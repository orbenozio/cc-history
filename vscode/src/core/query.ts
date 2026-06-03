// Port of cc_history.py query building + date parsing.
//
// Ground truth: ../../cc-history/cc_history.py — _FTS_OPERATOR_RE,
// build_fts_query, parse_duration_or_date (with the corrected UTC handling).

// Matches " * ( ) : or a standalone AND/OR/NOT/NEAR — same as the CLI regex
// `["*():]|(?:\b(?:AND|OR|NOT|NEAR)\b)`. If any of these appear, the query is
// treated as raw FTS5 syntax; otherwise it's auto-quoted as a single phrase.
const FTS_OPERATOR_RE = /["*():]|\b(?:AND|OR|NOT|NEAR)\b/;

export function buildFtsQuery(raw: string): string {
  if (FTS_OPERATOR_RE.test(raw)) {
    return raw; // explicit FTS5 syntax — pass through
  }
  const escaped = raw.replace(/"/g, '""');
  return `"${escaped}"`;
}

const DURATION_RE = /^(\d+)\s*([hdwm])$/i;

/**
 * Port of parse_duration_or_date. '1h'/'2d'/'1w'/'1m' → an offset from now;
 * otherwise an ISO date/datetime. Always returns a UTC "…Z" timestamp so string
 * comparisons against entries.ts (also UTC "Z") are correct across timezones:
 * a naive/bare date is interpreted as LOCAL wall-clock and converted to UTC; an
 * explicit offset is honored.
 */
export function parseDurationOrDate(value: string, now: Date = new Date()): string {
  const v = value.trim();
  const m = DURATION_RE.exec(v);
  if (m) {
    const n = parseInt(m[1], 10);
    const unit = m[2].toLowerCase();
    const ms =
      unit === "h"
        ? n * 3600_000
        : unit === "d"
        ? n * 86_400_000
        : unit === "w"
        ? n * 604_800_000
        : /* m */ n * 30 * 86_400_000;
    return toIsoZ(new Date(now.getTime() - ms));
  }
  const parsed = parseIsoLocal(v);
  if (parsed === null) {
    throw new Error(`Invalid --since/--until value: ${value}`);
  }
  return toIsoZ(parsed);
}

function toIsoZ(d: Date): string {
  // YYYY-MM-DDTHH:MM:SSZ (UTC, seconds precision) — matches the CLI format.
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

/**
 * Parse an ISO date/datetime like Python's datetime.fromisoformat: a value with
 * an explicit offset/Z is absolute; a naive one is LOCAL wall-clock. Returns a
 * Date, or null if unparseable. Accepts "YYYY-MM-DD" and
 * "YYYY-MM-DD[T ]HH:MM[:SS]" with optional offset.
 */
function parseIsoLocal(value: string): Date | null {
  // Date only.
  let m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (m) {
    const [, y, mo, d] = m;
    return new Date(+y, +mo - 1, +d); // local midnight
  }
  // Date + time, optional offset.
  m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:?\d{2})?$/.exec(
    value
  );
  if (!m) {
    return null;
  }
  const [, y, mo, d, hh, mi, ss, off] = m;
  if (off) {
    // Absolute time — normalize the offset to what Date can parse.
    const norm = off === "Z" ? "Z" : off.length === 5 ? `${off.slice(0, 3)}:${off.slice(3)}` : off;
    const iso = `${y}-${mo}-${d}T${hh}:${mi}:${ss ?? "00"}${norm}`;
    const dt = new Date(iso);
    return isNaN(dt.getTime()) ? null : dt;
  }
  // Naive → local wall-clock.
  return new Date(+y, +mo - 1, +d, +hh, +mi, ss ? +ss : 0);
}
