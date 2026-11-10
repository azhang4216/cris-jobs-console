BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS training_events (
    run_id TEXT NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    step INTEGER,
    timestamp TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS training_events_filter ON training_events(run_id, kind, seq);
CREATE INDEX IF NOT EXISTS training_events_step ON training_events(run_id, step, seq);
PRAGMA user_version = 2;
COMMIT;
