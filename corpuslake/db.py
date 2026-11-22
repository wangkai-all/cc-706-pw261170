"""Pure-Python SQLite driver so the tool runs where no third-party packages install.

This module exposes the small subset of DB-API 2.0 that corpuslake uses:

    db = Database(path)
    db.execute(sql, params) -> Cursor   (context-manager, iterable, fetchall/fetchone)
    db.executemany(sql, rows)
    db.executescript(sql)
    db.commit() / db.rollback() / db.close()
    db.backup(dest)                     (consistent copy for export)

Only ``?`` parameter style is used by this project, so that is all we translate.
Values may be str, int, float, bytes, bool, None (bytes are stored as BLOBs).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


class Row:
    """A row supporting index and name access, like sqlite3.Row."""

    __slots__ = ("_values", "_keys")

    def __init__(self, keys: Sequence[str], values: Sequence[Any]):
        self._keys = tuple(keys)
        self._values = tuple(values)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            try:
                return self._values[self._keys.index(key)]
            except ValueError:
                raise KeyError(key) from None
        return self._values[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return "Row(%s)" % ", ".join("%s=%r" % (k, v) for k, v in self.keys_dict().items())

    def keys(self) -> tuple[str, ...]:
        return self._keys

    def keys_dict(self) -> dict[str, Any]:
        return dict(zip(self._keys, self._values))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._values[self._keys.index(name)]
        except ValueError:
            raise AttributeError(name) from None


class Cursor:
    def __init__(self, db: "Database", cur: sqlite3.Cursor):
        self._db = db
        self._cur = cur

    @property
    def lastrowid(self) -> int:
        return self._cur.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def _row(self, raw: Sequence[Any] | None) -> Row | None:
        if raw is None:
            return None
        return Row(self._db._column_names(self._cur), raw)

    def fetchone(self) -> Row | None:
        return self._row(self._cur.fetchone())

    def fetchall(self) -> list[Row]:
        names = self._db._column_names(self._cur)
        return [Row(names, r) for r in self._cur.fetchall()]

    def fetchmany(self, size: int) -> list[Row]:
        names = self._db._column_names(self._cur)
        return [Row(names, r) for r in self._cur.fetchmany(size)]

    def __iter__(self) -> Iterator[Row]:
        names = self._db._column_names(self._cur)
        for raw in self._cur:
            yield Row(names, raw)

    def close(self) -> None:
        self._cur.close()

    def __enter__(self) -> "Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class Database:
    """Thin, explicit-transaction wrapper over sqlite3."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")

    def _column_names(self, cur: sqlite3.Cursor) -> tuple[str, ...]:
        if cur.description is None:
            return ()
        return tuple(d[0] for d in cur.description)

    @staticmethod
    def _adapt(params: Sequence[Any] | dict[str, Any] | None) -> Any:
        if params is None:
            return ()
        if isinstance(params, dict):
            return params
        out = []
        for v in params:
            if isinstance(v, bool):
                out.append(int(v))
            else:
                out.append(v)
        return out

    def execute(self, sql: str, params: Any = None) -> Cursor:
        return Cursor(self, self._conn.execute(sql, self._adapt(params)))

    def executemany(self, sql: str, rows: Iterable[Any]) -> Cursor:
        return Cursor(self, self._conn.executemany(sql, [self._adapt(r) for r in rows]))

    def executescript(self, script: str) -> None:
        self._conn.executescript(script)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def backup(self, dest: str | Path) -> None:
        target = sqlite3.connect(str(dest))
        try:
            self._conn.backup(target)
            target.commit()
        finally:
            target.close()

    # transaction helpers -------------------------------------------------
    def begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


_SCRIPT_SPLIT = re.compile(r";\s*(?:\n|$)")


def split_sql(script: str) -> list[str]:
    """Split a simple schema script into statements (no procedural bodies used)."""
    return [s.strip() for s in _SCRIPT_SPLIT.split(script) if s.strip()]
