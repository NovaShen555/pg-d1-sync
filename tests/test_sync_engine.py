from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import json
import sqlite3
import unittest

import psycopg

from app.config import Settings
from app.d1_client import D1Statement
from app.postgres_db import Column, Table
from app.sync_engine import META_TABLE, SHADOW_PREFIX, SyncEngine, SyncError


class FakeD1:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.fail_on_shadow_insert = False

    def query(self, sql: str, params=()):
        cur = self.db.execute(sql, params)
        rows = [dict(row) for row in cur.fetchall()] if cur.description else []
        self.db.commit()
        return rows

    def batch(self, statements: list[D1Statement]) -> None:
        with self.db:
            for item in statements:
                if self.fail_on_shadow_insert and item.sql.startswith("INSERT INTO \"" + SHADOW_PREFIX):
                    raise RuntimeError("simulated cloud failure")
                self.db.execute(item.sql, item.params)

    def close(self) -> None:
        self.db.close()


class FakePostgres:
    def __init__(self) -> None:
        self.psycopg = psycopg
        self.tables: dict[str, Table] = {}
        self.rows: dict[str, list[tuple]] = {}
        self.replace_count = 0

    @contextmanager
    def snapshot(self):
        yield

    def list_tables(self, included=()):
        return sorted(name for name in self.tables if not included or name in included)

    def get_table(self, name: str) -> Table:
        return self.tables[name]

    def iter_rows(self, table: Table):
        yield from self.rows.get(table.name, [])

    @contextmanager
    def replace_tables(self, specs, *, drop_tables=None):
        old_tables = self.tables.copy()
        old_rows = {name: list(rows) for name, rows in self.rows.items()}
        self.replace_count += 1
        for name in drop_tables or []:
            self.tables.pop(name, None)
            self.rows.pop(name, None)
        for spec in specs:
            self.tables[spec["name"]] = Table(
                spec["name"],
                tuple(Column(
                    col["name"], col["data_type"], col["pg_type"],
                    col["nullable"], col.get("auto_increment", False),
                ) for col in spec["columns"]),
                tuple(spec["primary_key"]),
            )
            self.rows[spec["name"]] = []
        try:
            yield
        except Exception:
            self.tables = old_tables
            self.rows = old_rows
            raise

    def insert_rows(self, spec, rows):
        self.rows[spec["name"]].extend(rows)


class TestSyncEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.d1 = FakeD1()
        self.pg = FakePostgres()
        self.pg.tables["items"] = Table(
            "items",
            (
                Column("id", "bigint", "bigint", False, True),
                Column("amount", "numeric", "numeric(20, 9)", False),
                Column("payload", "jsonb", "jsonb", True),
                Column("raw", "bytea", "bytea", True),
                Column("enabled", "boolean", "boolean", False),
            ),
            ("id",),
        )
        self.pg.rows["items"] = [
            (2**53 + 1, Decimal("123456789.123456789"), {"ok": True}, b"\x00\xff", True),
            (2, Decimal("0.100000000"), {"ok": False}, None, False),
        ]
        self.settings = Settings("postgresql://fake", "token", "account", "database", d1_page_size=1)
        self.engine = SyncEngine(self.settings, self.pg, self.d1)

    def tearDown(self) -> None:
        self.d1.close()

    def test_push_replaces_old_rows_without_exposing_half_built_table(self) -> None:
        self.assertEqual(self.engine.push_once(), 2)
        old = self.d1.query('SELECT id,amount,payload,raw,enabled FROM "items" ORDER BY id')
        self.assertEqual(old[1]["id"], 2**53 + 1)
        self.assertEqual(old[1]["amount"], "123456789.123456789")
        self.assertEqual(old[1]["raw"], b"\x00\xff")
        self.assertEqual(self.engine._read_metadata()["status"], "ready")

        self.pg.rows["items"] = [(3, Decimal("7.00"), {"new": 1}, b"Z", True)]
        self.assertEqual(self.engine.push_once(), 1)
        newer = self.d1.query('SELECT id,amount,raw FROM "items"')
        self.assertEqual(newer, [{"id": 3, "amount": "7.00", "raw": b"Z"}])
        self.assertEqual(self.d1.query("SELECT name FROM sqlite_master WHERE name LIKE '__pg_d1_sync_shadow_%'"), [])

    def test_one_shot_pull_restores_types_and_overwrites_local_rows(self) -> None:
        self.engine.push_once()
        self.pg.rows["items"] = [(99, Decimal(1), {}, b"", False)]
        self.assertEqual(self.engine.pull_once(), 2)
        self.assertEqual(self.pg.replace_count, 1)
        restored = self.pg.rows["items"]
        self.assertEqual([row[0] for row in restored], [2, 2**53 + 1])
        self.assertEqual(restored[1][1], Decimal("123456789.123456789"))
        self.assertEqual(restored[1][2].obj, {"ok": True})
        self.assertEqual(restored[1][3], b"\x00\xff")
        self.assertEqual(restored[1][4], True)

    def test_failed_push_preserves_previous_table_and_refuses_pull(self) -> None:
        self.engine.push_once()
        self.d1.fail_on_shadow_insert = True
        with self.assertRaisesRegex(RuntimeError, "simulated cloud failure"):
            self.engine.push_once()
        self.assertEqual(self.d1.query('SELECT COUNT(*) AS n FROM "items"'), [{"n": 2}])
        self.assertEqual(self.engine._read_metadata()["status"], "syncing")
        with self.assertRaisesRegex(SyncError, "complete snapshot"):
            self.engine.pull_once()
        self.assertEqual(self.pg.replace_count, 0)

    def test_empty_source_fails_without_erasing_cloud(self) -> None:
        self.engine.push_once()
        self.pg.tables.clear()
        self.pg.rows.clear()
        with self.assertRaisesRegex(SyncError, "No PostgreSQL tables"):
            self.engine.push_once()
        self.assertEqual(self.d1.query('SELECT COUNT(*) AS n FROM "items"'), [{"n": 2}])

    def test_pull_checks_manifest_before_overwriting(self) -> None:
        self.engine.push_once()
        self.d1.query('ALTER TABLE "items" ADD COLUMN "surprise" TEXT')
        with self.assertRaisesRegex(SyncError, "no longer matches"):
            self.engine.pull_once()
        self.assertEqual(self.pg.replace_count, 0)

    def test_existing_unowned_cloud_table_is_not_overwritten(self) -> None:
        self.d1.query('CREATE TABLE "items" ("id" INTEGER)')
        with self.assertRaisesRegex(SyncError, "not owned"):
            self.engine.push_once()
        self.assertEqual(self.d1.query("SELECT name FROM sqlite_master WHERE name='items'"), [{"name": "items"}])

    def test_partial_snapshot_requires_table_list_when_pulling(self) -> None:
        partial = SyncEngine(
            Settings("postgresql://fake", "token", "account", "database", sync_tables=("items",)),
            self.pg, self.d1,
        )
        partial.push_once()
        self.assertEqual(partial._read_metadata()["table_scope"], "selected")
        with self.assertRaisesRegex(SyncError, "SYNC_TABLES"):
            self.engine.pull_once()

    def test_subset_push_does_not_drop_unmanaged_d1_tables(self) -> None:
        self.d1.query('CREATE TABLE "keep_me" ("id" INTEGER)')
        partial = SyncEngine(
            Settings("postgresql://fake", "token", "account", "database", sync_tables=("items",)),
            self.pg, self.d1,
        )
        partial.push_once()
        self.assertEqual(self.d1.query("SELECT name FROM sqlite_master WHERE name='keep_me'"), [{"name": "keep_me"}])


if __name__ == "__main__":
    unittest.main()
