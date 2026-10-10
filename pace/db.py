"""SQLite access: one connection per thread, WAL, plain SQL.

Connections are in autocommit mode; writes that must be atomic go through
`db.tx()`, which takes the write lock up front (BEGIN IMMEDIATE) so concurrent
writers wait on busy_timeout instead of failing on lock upgrade.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS = Path(__file__).parent / "migrations"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=10000")
            self._local.conn = c
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        c = self.conn
        if c.in_transaction:  # nested: join the outer transaction
            yield c
            return
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    def migrate(self) -> None:
        c = self.conn
        have = c.execute("PRAGMA user_version").fetchone()[0]
        for f in sorted(MIGRATIONS.glob("*.sql")):
            n = int(f.name.split("_", 1)[0])
            if n > have:
                c.executescript(f"BEGIN; {f.read_text()}; PRAGMA user_version={n}; COMMIT;")

    # --- helpers ---

    def one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def run(self, sql: str, *args: Any) -> sqlite3.Cursor:
        return self.conn.execute(sql, args)

    def kv_get(self, key: str) -> Any:
        row = self.one("SELECT value FROM kv WHERE key = ?", key)
        return json.loads(row["value"]) if row else None

    def kv_set(self, key: str, value: Any) -> None:
        self.run("INSERT INTO kv(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, json.dumps(value))
