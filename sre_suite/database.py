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

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Rows:
        return self.connection.execute(query, parameters)

    @contextmanager
    def transaction(self, *, write: bool) -> Iterator[None]:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield

    def close(self) -> None:
        self.connection.close()
