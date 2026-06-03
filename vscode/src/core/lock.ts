// Cross-process advisory indexer lock (spec §4.4). Multiple VS Code windows are
// separate extension hosts; without this they would all index the same DB.
// Stored as a `meta` row so it works across processes and machines sharing a DB.
// A stale lock (owner crashed, heartbeat expired) is reclaimable.

import { SqliteDb } from "./sqliteEngine";

const LOCK_KEY = "indexer_lock";
export const LOCK_STALE_MS = 60_000; // reclaim if no heartbeat within this window

interface LockRecord {
  owner: string;
  pid: number;
  heartbeat_at: number; // epoch ms
}

function readLock(db: SqliteDb): LockRecord | null {
  const row = db.prepare("SELECT value FROM meta WHERE key = ?").get(LOCK_KEY) as
    | { value: string }
    | undefined;
  if (!row) {
    return null;
  }
  try {
    return JSON.parse(row.value) as LockRecord;
  } catch {
    return null;
  }
}

function writeLock(db: SqliteDb, rec: LockRecord): void {
  db.prepare(
    "INSERT INTO meta (key, value) VALUES (?, ?) " +
      "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
  ).run(LOCK_KEY, JSON.stringify(rec));
}

/** Try to acquire the lock for `owner`. Returns true if held by us afterward. */
export function tryAcquire(db: SqliteDb, owner: string, now = Date.now()): boolean {
  return db.immediateTransaction(() => {
    const cur = readLock(db);
    const free = !cur || now - cur.heartbeat_at > LOCK_STALE_MS || cur.owner === owner;
    if (!free) {
      return false;
    }
    writeLock(db, { owner, pid: process.pid, heartbeat_at: now });
    return true;
  });
}

/** Refresh our heartbeat; returns false if the lock was stolen. */
export function renew(db: SqliteDb, owner: string, now = Date.now()): boolean {
  return db.immediateTransaction(() => {
    const cur = readLock(db);
    if (!cur || cur.owner !== owner) {
      return false;
    }
    writeLock(db, { owner, pid: process.pid, heartbeat_at: now });
    return true;
  });
}

/** Release the lock if we still own it. */
export function release(db: SqliteDb, owner: string): void {
  db.immediateTransaction(() => {
    const cur = readLock(db);
    if (cur && cur.owner === owner) {
      db.prepare("DELETE FROM meta WHERE key = ?").run(LOCK_KEY);
    }
  });
}
