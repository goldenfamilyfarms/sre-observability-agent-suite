CREATE TABLE control_records (
    id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL, version BIGINT NOT NULL CHECK (version > 0)
);
CREATE TABLE control_events (
    sequence BIGINT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
    subject TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX control_events_scope ON control_events(scope, sequence);
CREATE TABLE control_operations (
    id TEXT PRIMARY KEY, scope TEXT NOT NULL, request_sha TEXT NOT NULL, payload TEXT NOT NULL
);
ALTER TABLE findings DROP CONSTRAINT findings_state_check;
ALTER TABLE findings ADD CONSTRAINT findings_state_check
    CHECK (state IN ('proposed', 'verified', 'disputed', 'corroborated', 'stale', 'superseded'));
ALTER TABLE events DROP CONSTRAINT events_state_check;
ALTER TABLE events ADD CONSTRAINT events_state_check
    CHECK (state IN ('verified', 'disputed', 'corroborated', 'stale', 'superseded'));

CREATE TABLE control_environments (id TEXT PRIMARY KEY, payload TEXT NOT NULL, closed INTEGER NOT NULL CHECK (closed IN (0, 1)));
