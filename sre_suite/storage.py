"""Explicit local schema adoption with lossless quarantine of unscoped v1 data."""

import sqlite3

LEGACY_TABLES = {
    "knowledge": ("metadata", "findings", "events"),
    "checkpoints": ("checkpoints", "operations"),
}


def tables(db: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def prepare_schema(db: sqlite3.Connection, component: str, statements: tuple[str, ...]) -> None:
    """All schema initialization is transactional, including the version marker."""
    with db:
        db.execute("BEGIN IMMEDIATE")
        existing = tables(db)
        version = None
        if "schema_versions" in existing:
            row = db.execute(
                "SELECT version FROM schema_versions WHERE component = ?", (component,)
            ).fetchone()
            version = row[0] if row else None
        if version is not None and version != 2:
            raise ValueError("unsupported storage version")
        if version is None and existing.intersection(LEGACY_TABLES.get(component, ())):
            raise ValueError("legacy unscoped database; explicitly quarantine_legacy first")
        db.execute(
            "CREATE TABLE IF NOT EXISTS schema_versions "
            "(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        for statement in statements:
            db.execute(statement)
        db.execute("INSERT OR IGNORE INTO schema_versions VALUES (?, 2)", (component,))


def quarantine_legacy(path: str) -> tuple[str, ...]:
    """Archive v1 tables in place; never guess environment IDs or rewrite old evidence.

    Stop writers and take a backup first. This function does not migrate v1 content
    into trusted v2 records. Operators must recollect scoped evidence separately.
    """
    db = sqlite3.connect(path)
    archived: list[str] = []
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            existing = tables(db)
            for component, names in LEGACY_TABLES.items():
                if "schema_versions" in existing:
                    row = db.execute(
                        "SELECT version FROM schema_versions WHERE component=?", (component,)
                    ).fetchone()
                    if row:
                        if row[0] != 2:
                            raise ValueError("unsupported storage version")
                        continue
                present = existing.intersection(names)
                if not present:
                    continue
                if present != set(names):
                    raise ValueError("partial legacy schema; no tables archived")
                for name in names:
                    target = f"legacy_v1_{name}"
                    if target in existing:
                        raise ValueError("legacy archive already exists")
                    # Identifiers come exclusively from the fixed table allowlist above.
                    db.execute(f'ALTER TABLE "{name}" RENAME TO "{target}"')
                    archived.append(target)
        return tuple(archived)
    finally:
        db.close()
