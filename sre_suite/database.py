"""Internal SQL port for fixed repository queries, not arbitrary user SQL.

Queries use qmark parameters. PostgreSQL translates these fixed statements; no
query contains a literal question mark. Values always travel separately.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol


class Rows(Protocol):
    def fetchone(self) -> tuple[Any, ...] | None: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...
    def __iter__(self) -> Iterator[tuple[Any, ...]]: ...


class Database(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Rows: ...
    def transaction(self, *, write: bool) -> AbstractContextManager[None]: ...
    def close(self) -> None: ...


class SQLiteDatabase:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._write: bool | None = None
        self._depth = 0

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Rows:
        return self.connection.execute(query, parameters)

    @contextmanager
    def transaction(self, *, write: bool) -> Iterator[None]:
        if self._write is not None:
            if write and not self._write:
                raise RuntimeError("cannot write inside a read transaction")
            self._depth += 1
            savepoint = f"sre_nested_{self._depth}"
            self.connection.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
            except BaseException:
                self.connection.execute(f"ROLLBACK TO {savepoint}")
                raise
            finally:
                self.connection.execute(f"RELEASE {savepoint}")
                self._depth -= 1
            return
        self._write = write
        try:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield
        finally:
            self._write = None

    def close(self) -> None:
        self.connection.close()
