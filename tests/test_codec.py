from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import unittest
from uuid import UUID

import psycopg

from app.codec import (
    UnsupportedTypeError, create_d1_table, decode_value, encode_value,
    insert_d1_row, quote_identifier, safe_postgres_type, sqlite_type,
)


class TestCodec(unittest.TestCase):
    def test_type_mapping_preserves_exact_decimal_and_encodes_binary(self) -> None:
        self.assertEqual(sqlite_type("numeric"), "TEXT")
        self.assertEqual(sqlite_type("bytea"), "BLOB")
        self.assertEqual(sqlite_type("bigint"), "INTEGER")
        self.assertEqual(encode_value(Decimal("123456789.123456789"), "numeric"), "123456789.123456789")
        self.assertEqual(encode_value(b"\x00\xff", "bytea"), b"\x00\xff")
        self.assertEqual(decode_value("00FF", "bytea", psycopg), b"\x00\xff")

    def test_high_precision_integer_is_bound_as_a_string(self) -> None:
        columns = [{"name": "id", "data_type": "bigint"}]
        item = insert_d1_row("items", columns, (2**53 + 1,))
        self.assertEqual(item.params, (str(2**53 + 1),))
        self.assertEqual(item.sql, 'INSERT INTO "items" ("id") VALUES (?)')

    def test_dates_and_json(self) -> None:
        when = datetime(2026, 10, 15, 13, 1, tzinfo=timezone.utc)
        self.assertEqual(encode_value(when, "timestamp with time zone"), when.isoformat())
        self.assertEqual(decode_value("2026-10-15", "date", psycopg), date(2026, 10, 15))
        self.assertEqual(encode_value({"x": [1, 2]}, "jsonb"), '{"x":[1,2]}')
        self.assertEqual(decode_value('{"x":1}', "jsonb", psycopg).obj, {"x": 1})
        self.assertEqual(decode_value("1", "boolean", psycopg), True)
        self.assertEqual(decode_value("e975bf67-15ab-4f69-a099-84ba03b8f04c", "uuid", psycopg),
                         UUID("e975bf67-15ab-4f69-a099-84ba03b8f04c"))

    def test_unsupported_type_and_non_finite_float_fail(self) -> None:
        with self.assertRaises(UnsupportedTypeError):
            sqlite_type("ARRAY")
        with self.assertRaises(UnsupportedTypeError):
            encode_value(float("nan"), "double precision")
        with self.assertRaises(UnsupportedTypeError):
            safe_postgres_type("TEXT); DROP TABLE users; --")

    def test_identifier_is_quoted(self) -> None:
        self.assertEqual(quote_identifier('a"b'), '"a""b"')
        with self.assertRaises(ValueError):
            quote_identifier("")

    def test_sqlite_schema(self) -> None:
        sql = create_d1_table("items", [
            {"name": "id", "sqlite_type": "INTEGER", "nullable": False},
            {"name": "text", "sqlite_type": "TEXT", "nullable": True},
        ], ["id"])
        self.assertIn('PRIMARY KEY ("id")', sql)
        self.assertIn('"id" INTEGER NOT NULL', sql)


if __name__ == "__main__":
    unittest.main()
