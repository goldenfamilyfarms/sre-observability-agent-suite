-- All persisted domain envelopes retain schema_version=2 and explicit Scope.
CREATE TABLE evidence_objects (
    id TEXT PRIMARY KEY,
    metadata TEXT NOT NULL,
    content BYTEA NOT NULL CHECK (octet_length(content) <= 1048576)
);
CREATE TABLE metadata (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version BIGINT NOT NULL CHECK (version >= 0)
);
INSERT INTO metadata VALUES (1, 0);
CREATE TABLE findings (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('proposed', 'verified', 'disputed'))
);
CREATE TABLE events (
    version BIGINT PRIMARY KEY CHECK (version > 0),
    finding_id TEXT NOT NULL REFERENCES findings(id),
    state TEXT NOT NULL CHECK (state IN ('verified', 'disputed'))
);
CREATE TABLE previews (
    environment_id TEXT PRIMARY KEY CHECK (
        length(environment_id) <= 63 AND
        environment_id ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'
    ),
    payload TEXT NOT NULL,
    closed INTEGER NOT NULL CHECK (closed IN (0, 1))
);
CREATE TABLE preview_events (
    sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    environment_id TEXT NOT NULL REFERENCES previews(environment_id),
    generation BIGINT NOT NULL CHECK (generation > 0),
    kind TEXT NOT NULL CHECK (kind IN ('requested', 'closed')),
    at TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE checkpoints (
    run_id TEXT PRIMARY KEY,
    version BIGINT NOT NULL CHECK (version > 0),
    payload TEXT NOT NULL,
    scope TEXT NOT NULL
);
CREATE TABLE operations (
    run_id TEXT NOT NULL REFERENCES checkpoints(run_id),
    operation_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    expected_version BIGINT NOT NULL CHECK (expected_version >= 0),
    result_version BIGINT NOT NULL CHECK (result_version = expected_version + 1),
    PRIMARY KEY(run_id, operation_id)
);
