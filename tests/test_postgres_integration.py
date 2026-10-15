from __future__ import annotations

from decimal import Decimal
import os
import unittest
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.config import Settings
from app.postgres_db import PostgresDatabase
from app.sync_engine import SyncEngine
from test_sync_engine import FakeD1


@unittest.skipUnless(os.getenv("TEST_POSTGRES_DSN"), "TEST_POSTGRES_DSN is not set")
class TestPostgresIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = os.environ["TEST_POSTGRES_DSN"]
        self.schema = "pgd1_" + uuid4().hex[:12]
        self.admin = psycopg.connect(self.dsn, autocommit=True)
        self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.admin.execute(
            f'''CREATE TABLE "{self.schema}"."items" (
                id BIGSERIAL PRIMARY KEY,
                amount NUMERIC(20, 9) NOT NULL,
                payload JSONB,
                raw BYTEA,
                enabled BOOLEAN NOT NULL
            )'''
        )
        self.admin.execute(
            f'INSERT INTO "{self.schema}"."items" '
            '(id,amount,payload,raw,enabled) VALUES (%s,%s,%s,%s,%s)',
            (2**53 + 1, Decimal("123456789.123456789"), Jsonb({"ok": True}), b"\x00\xff", True),
        )
        self.pg = PostgresDatabase(self.dsn, self.schema)
        self.pg.acquire_lock()
        self.d1 = FakeD1()
        self.settings = Settings(self.dsn, "fake", "fake", "fake", postgres_schema=self.schema)
        self.engine = SyncEngine(self.settings, self.pg, self.d1)

    def tearDown(self) -> None:
        self.pg.close()
        self.d1.close()
        self.admin.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        self.admin.close()

    def test_real_postgres_push_and_destructive_pull(self) -> None:
        self.assertEqual(self.engine.push_once(), 1)
        # The local database is now different. Pull must replace it from D1.
        self.admin.execute(f'TRUNCATE "{self.schema}"."items"')
        self.admin.execute(
            f'INSERT INTO "{self.schema}"."items" '
            '(id,amount,payload,raw,enabled) VALUES (%s,%s,%s,%s,%s)',
            (99, Decimal("1.00"), Jsonb({"bad": True}), b"x", False),
        )
        self.assertEqual(self.engine.pull_once(), 1)
        with self.admin.cursor() as cur:
            cur.execute(
                f'SELECT id,amount,payload,raw,enabled FROM "{self.schema}"."items"'
            )
            self.assertEqual(cur.fetchall(), [
                (2**53 + 1, Decimal("123456789.123456789"), {"ok": True}, b"\x00\xff", True)
            ])
            cur.execute(
                f'INSERT INTO "{self.schema}"."items" '
                '(amount,payload,enabled) VALUES (%s,%s,%s) RETURNING id',
                (Decimal("0"), Jsonb({"next": True}), True),
            )
            self.assertGreater(cur.fetchone()[0], 2**53 + 1)


if __name__ == "__main__":
    unittest.main()
