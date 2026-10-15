from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from typing import Any, Iterator, Sequence
from uuid import uuid4

from .codec import (
    create_d1_table,
    decode_value,
    insert_d1_row,
    quote_identifier,
    safe_postgres_type,
    sqlite_type,
)
from .config import Settings
from .d1_client import D1Client, D1Statement
from .postgres_db import PostgresDatabase, Table

LOGGER = logging.getLogger(__name__)
META_TABLE = "__pg_d1_sync_meta"
SHADOW_PREFIX = "__pg_d1_sync_shadow_"
FORMAT_VERSION = 1


class SyncError(RuntimeError):
    """Raised when a source/target state cannot be synchronized safely."""


class SyncEngine:
    def __init__(self, settings: Settings, postgres: PostgresDatabase, d1: D1Client) -> None:
        self.settings = settings
        self.postgres = postgres
        self.d1 = d1

    def push_once(self) -> int:
        with self.postgres.snapshot():
            names = self.postgres.list_tables(self.settings.sync_tables)
            if not names:
                raise SyncError("No PostgreSQL tables selected; refusing to erase the D1 mirror")
            if any(name == META_TABLE or name.startswith(SHADOW_PREFIX) for name in names):
                raise SyncError("A PostgreSQL table uses a reserved D1 synchronization name")
            tables = [self.postgres.get_table(name) for name in names]
            manifest = [table.manifest() for table in tables]
            _validate_manifest(manifest)

            old = self._read_metadata()
            existing = self._d1_table_names()
            old_managed = _managed_names(old)
            collisions = sorted(set(names).intersection(existing.difference(old_managed)))
            if collisions:
                raise SyncError(
                    "D1 contains tables not owned by this synchronizer: " + ", ".join(collisions)
                )
            if old is not None and old.get("schema") != self.settings.postgres_schema:
                raise SyncError("D1 is already bound to a different PostgreSQL schema")

            generation = uuid4().hex
            if self.settings.sync_tables and old is not None:
                selected_names = set(names)
                prior_manifest = [
                    spec for spec in old.get("tables", [])
                    if isinstance(spec, dict) and spec.get("name") not in selected_names
                ]
                effective_manifest = prior_manifest + manifest
            else:
                effective_manifest = manifest
            full_scope = (
                not self.settings.sync_tables
                or (old is not None and old.get("table_scope") == "schema")
            )
            state = {
                "format_version": FORMAT_VERSION,
                "table_scope": "schema" if full_scope else "selected",
                "status": "syncing",
                "generation": generation,
                "schema": self.settings.postgres_schema,
                "tables": effective_manifest,
                "previous_tables": sorted(old_managed),
                "updated_at": _now(),
            }
            self._write_metadata(state)

            total = 0
            for table, spec in zip(tables, manifest):
                shadow = _shadow_name(table.name)
                self.d1.query(f"DROP TABLE IF EXISTS {quote_identifier(shadow)}")
                self.d1.query(create_d1_table(shadow, spec["columns"], spec["primary_key"]))
                count = self._fill_shadow(shadow, table, spec)
                # D1's batch API executes these statements as a transaction:
                # readers retain the previous table until the new table is ready.
                self.d1.batch(
                    [
                        D1Statement(f"DROP TABLE IF EXISTS {quote_identifier(table.name)}"),
                        D1Statement(
                            f"ALTER TABLE {quote_identifier(shadow)} "
                            f"RENAME TO {quote_identifier(table.name)}"
                        ),
                    ]
                )
                LOGGER.info("Pushed %s: %d rows", table.name, count)
                total += count

            current_names = {spec["name"] for spec in state["tables"]}
            removed = old_managed.difference(current_names)
            for removed_name in sorted(removed):
                self.d1.query(f"DROP TABLE IF EXISTS {quote_identifier(removed_name)}")
            state["status"] = "ready"
            state["updated_at"] = _now()
            state.pop("previous_tables", None)
            self._write_metadata(state)
            LOGGER.info("Push complete: %d tables, %d rows", len(tables), total)
            return total

    def pull_once(self) -> int:
        state = self._read_metadata()
        if state is None or state.get("status") != "ready":
            raise SyncError("D1 has no complete snapshot; pull is refused")
        if state.get("schema") != self.settings.postgres_schema:
            raise SyncError("POSTGRES_SCHEMA does not match the D1 snapshot")
        if state.get("table_scope") == "selected" and not self.settings.sync_tables:
            raise SyncError("This D1 snapshot was created with SYNC_TABLES; configure a table list before pulling")
        manifest = state.get("tables")
        if not isinstance(manifest, list):
            raise SyncError("D1 snapshot has no table manifest")
        _validate_manifest(manifest)
        requested = set(self.settings.sync_tables)
        if requested:
            unknown = requested.difference(spec["name"] for spec in manifest)
            if unknown:
                raise SyncError("SYNC_TABLES not in D1 snapshot: " + ", ".join(sorted(unknown)))
            selected = [spec for spec in manifest if spec["name"] in requested]
        else:
            selected = manifest
        if not selected:
            raise SyncError("D1 snapshot contains no selected tables")

        LOGGER.warning(
            "Pull will drop and recreate %d table(s) in PostgreSQL schema %s",
            len(selected), self.settings.postgres_schema,
        )
        with TemporaryDirectory(prefix="pg-d1-sync-") as directory:
            stage = sqlite3.connect(str(Path(directory) / "snapshot.sqlite"))
            try:
                stage.execute(
                    "CREATE TABLE rows (table_name TEXT NOT NULL, position INTEGER NOT NULL, "
                    "payload TEXT NOT NULL, PRIMARY KEY(table_name, position))"
                )
                for spec in selected:
                    self._stage_table(stage, spec)
                stage.commit()

                refreshed = self._read_metadata()
                if (
                    refreshed is None
                    or refreshed.get("status") != "ready"
                    or refreshed.get("generation") != state.get("generation")
                ):
                    raise SyncError("D1 snapshot changed during download; PostgreSQL was not touched")

                local_to_drop = (
                    list(self.settings.sync_tables)
                    if self.settings.sync_tables
                    else self.postgres.list_tables()
                )
                total = 0
                with self.postgres.replace_tables(selected, drop_tables=local_to_drop):
                    for spec in selected:
                        total += self._restore_table(stage, spec)
                LOGGER.info("Pull complete: %d tables, %d rows", len(selected), total)
                return total
            finally:
                stage.close()

    def _fill_shadow(self, shadow: str, table: Table, spec: dict[str, Any]) -> int:
        count = 0
        pending: list[D1Statement] = []
        for row in self.postgres.iter_rows(table):
            pending.append(insert_d1_row(shadow, spec["columns"], row))
            count += 1
            if len(pending) >= self.settings.d1_batch_size:
                self.d1.batch(pending)
                pending = []
        if pending:
            self.d1.batch(pending)
        return count

    def _stage_table(self, stage: sqlite3.Connection, spec: dict[str, Any]) -> None:
        table = spec["name"]
        info = self.d1.query(f"PRAGMA table_info({quote_identifier(table)})")
        expected = spec["columns"]
        if len(info) != len(expected) or any(
            str(actual.get("name")) != col["name"]
            or str(actual.get("type", "")).upper() != col["sqlite_type"]
            for actual, col in zip(info, expected)
        ):
            raise SyncError(f"D1 table {table!r} no longer matches the snapshot manifest")
        fields = []
        for col in expected:
            name = quote_identifier(col["name"])
            if col["data_type"] == "bytea":
                expression = f"hex({name})"
            elif col["sqlite_type"] == "INTEGER":
                # Preserve int64 exactly across the REST API's JSON number path.
                expression = f"CAST({name} AS TEXT)"
            else:
                expression = name
            fields.append(f"{expression} AS {name}")
        order = spec["primary_key"]
        order_sql = ", ".join(quote_identifier(part) for part in order) if order else "rowid"
        count_rows = self.d1.query(f"SELECT COUNT(*) AS n FROM {quote_identifier(table)}")
        if len(count_rows) != 1:
            raise SyncError(f"D1 table {table!r} did not return a count")
        expected_count = int(count_rows[0]["n"])
        offset = 0
        page_size = self.settings.d1_page_size
        while offset < expected_count:
            sql = (
                f"SELECT {', '.join(fields)} FROM {quote_identifier(table)} "
                f"ORDER BY {order_sql} LIMIT {page_size} OFFSET {offset}"
            )
            rows = self.d1.query(sql)
            if not rows:
                break
            values = [
                (table, offset + index, json.dumps(
                    [row.get(col["name"]) for col in expected],
                    ensure_ascii=False, separators=(",", ":"),
                ))
                for index, row in enumerate(rows)
            ]
            stage.executemany(
                "INSERT INTO rows (table_name, position, payload) VALUES (?, ?, ?)", values
            )
            offset += len(rows)
        if offset != expected_count:
            raise SyncError(f"D1 table {table!r} changed during download")
        LOGGER.info("Downloaded %s: %d rows", table, offset)

    def _restore_table(self, stage: sqlite3.Connection, spec: dict[str, Any]) -> int:
        table = spec["name"]
        columns = spec["columns"]
        batch: list[tuple[Any, ...]] = []
        count = 0
        for (payload,) in stage.execute(
            "SELECT payload FROM rows WHERE table_name = ? ORDER BY position", (table,)
        ):
            raw = json.loads(payload)
            row = tuple(
                decode_value(value, col["data_type"], self.postgres.psycopg)
                for value, col in zip(raw, columns)
            )
            batch.append(row)
            if len(batch) >= 200:
                self.postgres.insert_rows(spec, batch)
                count += len(batch)
                batch = []
        if batch:
            self.postgres.insert_rows(spec, batch)
            count += len(batch)
        LOGGER.info("Restored %s: %d rows", table, count)
        return count

    def _d1_table_names(self) -> set[str]:
        rows = self.d1.query("SELECT name FROM sqlite_master WHERE type='table'")
        return {
            str(row["name"])
            for row in rows
            if not str(row["name"]).startswith("sqlite_")
            and not str(row["name"]).startswith(SHADOW_PREFIX)
            and str(row["name"]) != META_TABLE
        }

    def _read_metadata(self) -> dict[str, Any] | None:
        rows = self.d1.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (META_TABLE,)
        )
        if not rows:
            return None
        result = self.d1.query(f"SELECT payload FROM {quote_identifier(META_TABLE)} WHERE id=1")
        if len(result) != 1:
            raise SyncError("D1 metadata table is missing its snapshot record")
        try:
            state = json.loads(result[0]["payload"])
        except (ValueError, TypeError) as exc:
            raise SyncError("D1 metadata is invalid JSON") from exc
        if not isinstance(state, dict) or state.get("format_version") != FORMAT_VERSION:
            raise SyncError("Unsupported D1 snapshot format")
        return state

    def _write_metadata(self, state: dict[str, Any]) -> None:
        self.d1.query(
            f"CREATE TABLE IF NOT EXISTS {quote_identifier(META_TABLE)} "
            "(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        self.d1.query(
            f"INSERT INTO {quote_identifier(META_TABLE)} (id,payload) VALUES (1,?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (payload,),
        )


def _validate_manifest(manifest: Sequence[dict[str, Any]]) -> None:
    seen_tables: set[str] = set()
    for spec in manifest:
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
            raise SyncError("Invalid table manifest")
        name = spec["name"]
        if not name or name in seen_tables or name == META_TABLE or name.startswith(SHADOW_PREFIX):
            raise SyncError(f"Duplicate or reserved table name: {name!r}")
        seen_tables.add(name)
        columns = spec.get("columns")
        if not isinstance(columns, list) or not 1 <= len(columns) <= 100:
            raise SyncError(f"Invalid column manifest for {name!r}")
        seen_columns: set[str] = set()
        for col in columns:
            if not isinstance(col, dict) or not isinstance(col.get("name"), str):
                raise SyncError(f"Invalid column in {name!r}")
            if col["name"] in seen_columns or not col["name"]:
                raise SyncError(f"Duplicate column in {name!r}: {col['name']!r}")
            seen_columns.add(col["name"])
            if col.get("sqlite_type") != sqlite_type(str(col.get("data_type", ""))):
                raise SyncError(f"Type mismatch in D1 manifest: {name}.{col['name']}")
            safe_postgres_type(str(col.get("pg_type", "")))
            if not isinstance(col.get("nullable"), bool):
                raise SyncError(f"Invalid nullable flag: {name}.{col['name']}")
        pk = spec.get("primary_key")
        if not isinstance(pk, list) or len(pk) != len(set(pk)) or not set(pk).issubset(seen_columns):
            raise SyncError(f"Invalid primary key in {name!r}")


def _managed_names(state: dict[str, Any] | None) -> set[str]:
    if state is None:
        return set()
    tables = state.get("tables") or []
    previous = state.get("previous_tables") or []
    if not isinstance(tables, list) or not isinstance(previous, list):
        raise SyncError("Invalid D1 metadata table list")
    return {spec["name"] for spec in tables if isinstance(spec, dict) and "name" in spec} | set(previous)


def _shadow_name(name: str) -> str:
    return SHADOW_PREFIX + hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
