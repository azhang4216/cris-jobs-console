CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hook_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    canonical INTEGER NOT NULL DEFAULT 1 CHECK(canonical IN (0, 1)),
    received_at TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    run_id TEXT,
    payload TEXT NOT NULL,
    UNIQUE(hook_id, delivery_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS canonical_payload ON deliveries(body_hash) WHERE canonical = 1;
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    delivery_row_id INTEGER UNIQUE REFERENCES deliveries(id),
    state TEXT NOT NULL,
    capacity_reserved INTEGER NOT NULL DEFAULT 0 CHECK(capacity_reserved IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_queue ON runs(state, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS single_capacity_slot ON runs(capacity_reserved) WHERE capacity_reserved = 1;
CREATE TABLE IF NOT EXISTS scheduler_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    cluster TEXT NOT NULL,
    job_id TEXT NOT NULL,
    data TEXT NOT NULL,
    UNIQUE(cluster, job_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT REFERENCES runs(id),
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
PRAGMA user_version = 1;
