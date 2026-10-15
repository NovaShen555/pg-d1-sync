from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
import json
import math
import re
from typing import Any, Sequence
from uuid import UUID

from .d1_client import D1Statement


class UnsupportedTypeError(ValueError):
    """Raised instead of silently corrupting an unsupported PostgreSQL value."""


_PG_TYPES: dict[str, str] = {
    "smallint": "INTEGER", "integer": "INTEGER", "bigint": "INTEGER", "boolean": "INTEGER",
    "real": "REAL", "double precision": "REAL", "numeric": "TEXT",
    "text": "TEXT", "character varying": "TEXT", "character": "TEXT",
    "uuid": "TEXT", "json": "TEXT", "jsonb": "TEXT", "bytea": "BLOB",
    "date": "TEXT", "time without time zone": "TEXT", "time with time zone": "TEXT",
    "timestamp without time zone": "TEXT", "timestamp with time zone": "TEXT",
}
_SAFE_PG_TYPE = re.compile(
    r"^(?:SMALLINT|INTEGER|BIGINT|BOOLEAN|REAL|DOUBLE PRECISION|TEXT|UUID|JSONB?|BYTEA|DATE|"
    r"TIME(?:\(\d+\))?(?: WITH(?:OUT)? TIME ZONE)?|"
    r"TIMESTAMP(?:\(\d+\))?(?: WITH(?:OUT)? TIME ZONE)?|"
    r"CHARACTER(?: VARYING)?(?:\(\d+\))?|NUMERIC(?:\(\d+(?:,\s*\d+)?\))?)$",
    re.IGNORECASE,
)


def quote_identifier(name: str) -> str:
    if not name or "\x00" in name:
        raise ValueError("Invalid SQL identifier")
    return '"' + name.replace('"', '""') + '"'


def sqlite_type(data_type: str) -> str:
    try:
        return _PG_TYPES[data_type.lower()]
    except KeyError as exc:
        raise UnsupportedTypeError(f"Unsupported PostgreSQL type: {data_type}") from exc


def safe_postgres_type(type_name: str) -> str:
    if not _SAFE_PG_TYPE.fullmatch(type_name):
        raise UnsupportedTypeError(f"Unsafe or unsupported PostgreSQL type: {type_name!r}")
    return type_name


def encode_value(value: Any, data_type: str) -> str | None:
    if value is None:
        return None
    kind = data_type.lower()
    if kind == "bytea":
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise UnsupportedTypeError("Expected bytes for PostgreSQL bytea")
        return bytes(value)
    if kind == "boolean":
        return "1" if value else "0"
    if kind in {"json", "jsonb"}:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if isinstance(value, float) and not math.isfinite(value):
        raise UnsupportedTypeError("NaN and Infinity are not supported by D1 REST JSON")
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID, int, float, str)):
        return str(value)
    raise UnsupportedTypeError(f"Cannot encode {type(value).__name__} as {data_type}")


def decode_value(value: Any, data_type: str, psycopg: Any) -> Any:
    if value is None:
        return None
    kind = data_type.lower()
    if kind == "bytea":
        try:
            return bytes.fromhex(str(value))
        except ValueError as exc:
            raise UnsupportedTypeError("D1 BLOB is not valid hex") from exc
    if kind == "boolean":
        return str(value).lower() in {"1", "t", "true"}
    if kind in {"json", "jsonb"}:
        parsed = json.loads(str(value))
        wrapper = psycopg.types.json.Jsonb if kind == "jsonb" else psycopg.types.json.Json
        return wrapper(parsed)
    if kind == "date":
        return date.fromisoformat(str(value))
    if kind.startswith("timestamp"):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if kind.startswith("time"):
        return time.fromisoformat(str(value))
    if kind == "uuid":
        return UUID(str(value))
    if kind == "numeric":
        return Decimal(str(value))
    if kind in {"smallint", "integer", "bigint"}:
        return int(value)
    if kind in {"real", "double precision"}:
        return float(value)
    return str(value)


def create_d1_table(name: str, columns: Sequence[dict[str, Any]], primary_key: Sequence[str]) -> str:
    parts = []
    for col in columns:
        piece = quote_identifier(col["name"]) + " " + col["sqlite_type"]
        if not col["nullable"]:
            piece += " NOT NULL"
        parts.append(piece)
    if not parts or len(parts) > 100:
        raise UnsupportedTypeError("D1 tables require 1 to 100 columns")
    if primary_key:
        parts.append("PRIMARY KEY (" + ", ".join(quote_identifier(p) for p in primary_key) + ")")
    return f"CREATE TABLE {quote_identifier(name)} (" + ", ".join(parts) + ")"


def insert_d1_row(name: str, columns: Sequence[dict[str, Any]], values: Sequence[Any]) -> D1Statement:
    if len(columns) != len(values):
        raise ValueError("Column and value counts differ")
    params: list[str] = []
    placeholders: list[str] = []
    for col, value in zip(columns, values):
        encoded = encode_value(value, col["data_type"])
        if encoded is None:
            placeholders.append("NULL")
        elif col["data_type"].lower() == "bytea":
            placeholders.append("X'" + encoded.hex() + "'")
        else:
            placeholders.append("?")
            params.append(encoded)
    statement = (
        f"INSERT INTO {quote_identifier(name)} ("
        + ", ".join(quote_identifier(col["name"]) for col in columns)
        + ") VALUES (" + ", ".join(placeholders) + ")"
    )
    if len(params) > 100 or len(statement.encode("utf-8")) > 100_000:
        raise UnsupportedTypeError("D1 query exceeds parameter or SQL length limits (BLOBs are hex literals)")
    return D1Statement(statement, tuple(params))
