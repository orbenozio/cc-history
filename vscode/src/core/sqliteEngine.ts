// SqliteEngine seam. v1 ships the better-sqlite3 (native) engine. A read-only
// sql.js (WASM) fallback can implement the same surface later for the
// ABI-mismatch degraded path (spec §3.4) — it would throw on openReadWrite,
// hence the `canWrite` flag.

export interface SqliteStatement {
  run(...params: unknown[]): { changes: number; lastInsertRowid: number | bigint };
  get(...params: unknown[]): unknown;
  all(...params: unknown[]): unknown[];
}

export interface SqliteDb {
  readonly canWrite: boolean;
  exec(sql: string): void;
  prepare(sql: string): SqliteStatement;
  pragma(source: string): unknown;
  /** Run fn inside a single BEGIN IMMEDIATE … COMMIT (rollback on throw). */
  immediateTransaction<T>(fn: () => T): T;
  close(): void;
}

export interface SqliteEngine {
  readonly canWrite: boolean;
  openReadWrite(dbPath: string): SqliteDb;
  openReadOnly(dbPath: string): SqliteDb;
}

// --- better-sqlite3 implementation ---------------------------------------

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type BetterDb = any;

class BetterSqliteDb implements SqliteDb {
  readonly canWrite: boolean;
  private db: BetterDb;

  constructor(db: BetterDb, canWrite: boolean) {
    this.db = db;
    this.canWrite = canWrite;
  }

  exec(sql: string): void {
    this.db.exec(sql);
  }

  prepare(sql: string): SqliteStatement {
    return this.db.prepare(sql);
  }

  pragma(source: string): unknown {
    return this.db.pragma(source);
  }

  immediateTransaction<T>(fn: () => T): T {
    // better-sqlite3's .immediate() wrapper issues BEGIN IMMEDIATE and rolls
    // back automatically if fn throws — matching the CLI's per-file transaction.
    return this.db.transaction(fn).immediate();
  }

  close(): void {
    this.db.close();
  }
}

export class BetterSqlite3Engine implements SqliteEngine {
  readonly canWrite = true;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private Database: any;

  constructor() {
    // Loaded lazily so a host without the native binary can still construct
    // a read-only fallback engine instead.
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    this.Database = require("better-sqlite3");
  }

  openReadWrite(dbPath: string): SqliteDb {
    const db = new this.Database(dbPath);
    db.pragma("journal_mode = WAL");
    db.pragma("synchronous = NORMAL");
    db.pragma("foreign_keys = ON");
    return new BetterSqliteDb(db, true);
  }

  openReadOnly(dbPath: string): SqliteDb {
    const db = new this.Database(dbPath, { readonly: true, fileMustExist: true });
    return new BetterSqliteDb(db, false);
  }
}
