from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence
from uuid import uuid4

from .codec import quote_identifier, safe_postgres_type, sqlite_type


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    pg_type: str
    nullable: bool
    auto_increment: bool = False

    def manifest(self) -> dict[str, Any]:
        safe_postgres_type(self.pg_type)
        return {
            "name": self.name,
            "data_type": self.data_type,
            "pg_type": self.pg_type,
            "sqlite_type": sqlite_type(self.data_type),
            "nullable": self.nullable,
            "auto_increment": self.auto_increment,
        }


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": [col.manifest() for col in self.columns],
            "primary_key": list(self.primary_key),
        }


class PostgresError(RuntimeError):
    """A PostgreSQL operation cannot complete safely."""


class PostgresDatabase:
    def __init__(self, dsn: str, schema: str) -> None:
        import psycopg
        self.psycopg = psycopg
        self.schema = schema
        self.connection = psycopg.connect(dsn, autocommit=True)

    def close(self) -> None:
        self.connection.close()

    def acquire_lock(self) -> None:
        # Session-level advisory lock excludes two synchronizers connected to
        # the same PostgreSQL database without locking application tables.
        with self.connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(54709391, 120126147)")
            if not cur.fetchone()[0]:
                raise PostgresError("Another synchronizer holds the PostgreSQL advisory lock")

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        with self.connection.transaction():
            self.connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield

    def list_tables(self, included: Sequence[str] = ()) -> list[str]:
        with self.connection.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind = 'r' AND NOT c.relispartition
                ORDER BY c.relname
                """,
                (self.schema,),
            )
            found = [row[0] for row in cur.fetchall()]
        if included:
            missing = set(included).difference(found)
            if missing:
                raise PostgresError("SYNC_TABLES not found in PostgreSQL: " + ", ".join(sorted(missing)))
            return [name for name in found if name in included]
        return found

    def get_table(self, name: str) -> Table:
        with self.connection.cursor() as cur:
            cur.execute(
                """
                SELECT a.attname, info.data_type,
                       pg_catalog.format_type(a.atttypid, a.atttypmod) AS pg_type,
                       NOT a.attnotnull AS nullable, a.attgenerated,
                       a.attidentity,
                       pg_catalog.pg_get_expr(def.adbin, def.adrelid) AS default_expr
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid
                JOIN information_schema.columns AS info
                  ON info.table_schema = n.nspname
                 AND info.table_name = c.relname
                 AND info.column_name = a.attname
                LEFT JOIN pg_catalog.pg_attrdef AS def
                  ON def.adrelid = c.oid AND def.adnum = a.attnum
                WHERE n.nspname = %s AND c.relname = %s
                  AND c.relkind = 'r' AND NOT c.relispartition
                  AND a.attnum > 0 AND NOT a.attisdropped
                ORDER BY a.attnum
                """,
                (self.schema, name),
            )
            raw = cur.fetchall()
            if not raw:
                raise PostgresError(f"PostgreSQL table has no readable columns: {name}")
            columns: list[Column] = []
            for col_name, data_type, pg_type, nullable, generated, identity, default in raw:
                if generated:
                    raise PostgresError(f"Generated column {name}.{col_name} is not supported")
                # This validates supported types before any D1 table is touched.
                sqlite_type(data_type)
                safe_postgres_type(pg_type)
                auto_increment = bool(identity) or bool(default and default.startswith("nextval("))
                columns.append(Column(col_name, data_type, pg_type, nullable, auto_increment))
            cur.execute(
                """
                SELECT a.attname
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_index AS i ON i.indrelid = c.oid AND i.indisprimary
                JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS keys(attnum, pos) ON TRUE
                JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid AND a.attnum = keys.attnum
                WHERE n.nspname = %s AND c.relname = %s
                ORDER BY keys.pos
                """,
                (self.schema, name),
            )
            primary_key = tuple(row[0] for row in cur.fetchall())
        return Table(name, tuple(columns), primary_key)

    def iter_rows(self, table: Table, fetch_size: int = 500) -> Iterator[tuple[Any, ...]]:
        fields = ", ".join(quote_identifier(col.name) for col in table.columns)
        sql = f"SELECT {fields} FROM {quote_identifier(self.schema)}.{quote_identifier(table.name)}"
        with self.connection.cursor(name=f"pg_d1_{uuid4().hex}") as cur:
            cur.execute(sql)
            while True:
                batch = cur.fetchmany(fetch_size)
                if not batch:
                    break
                yield from batch

    @contextmanager
    def replace_tables(
        self,
        specs: Sequence[dict[str, Any]],
        *,
        drop_tables: Sequence[str] | None = None,
    ) -> Iterator[None]:
        names = [spec["name"] for spec in specs]
        tables_to_drop = list(drop_tables) if drop_tables is not None else names
        with self.connection.transaction():
            with self.connection.cursor() as cur:
                if tables_to_drop:
                    cur.execute(
                        """
                        SELECT child_ns.nspname, child.relname, parent.relname
                        FROM pg_catalog.pg_constraint AS fk
                        JOIN pg_catalog.pg_class AS child ON child.oid = fk.conrelid
                        JOIN pg_catalog.pg_namespace AS child_ns ON child_ns.oid = child.relnamespace
                        JOIN pg_catalog.pg_class AS parent ON parent.oid = fk.confrelid
                        JOIN pg_catalog.pg_namespace AS parent_ns ON parent_ns.oid = parent.relnamespace
                        WHERE fk.contype = 'f' AND parent_ns.nspname = %s
                          AND parent.relname = ANY(%s)
                        """,
                        (self.schema, tables_to_drop),
                    )
                    for child_schema, child, parent in cur.fetchall():
                        if child_schema != self.schema or child not in tables_to_drop:
                            raise PostgresError(
                                f"Cannot replace {parent}: external table {child_schema}.{child} references it"
                            )
                    cur.execute(
                        """
                        SELECT DISTINCT view_ns.nspname, view.relname
                        FROM pg_catalog.pg_depend AS dep
                        JOIN pg_catalog.pg_rewrite AS rw
                          ON dep.classid = 'pg_catalog.pg_rewrite'::regclass AND dep.objid = rw.oid
                        JOIN pg_catalog.pg_class AS view ON view.oid = rw.ev_class
                        JOIN pg_catalog.pg_namespace AS view_ns ON view_ns.oid = view.relnamespace
                        JOIN pg_catalog.pg_class AS target ON target.oid = dep.refobjid
                        JOIN pg_catalog.pg_namespace AS target_ns ON target_ns.oid = target.relnamespace
                        WHERE target_ns.nspname = %s AND target.relname = ANY(%s)
                          AND view.relkind IN ('v', 'm')
                        """,
                        (self.schema, tables_to_drop),
                    )
                    dependent_views = cur.fetchall()
                    if dependent_views:
                        view_names = ", ".join(f"{schema}.{name}" for schema, name in dependent_views)
                        raise PostgresError(
                            "Cannot replace tables referenced by views/materialized views: " + view_names
                        )
                for name in sorted(set(tables_to_drop), reverse=True):
                    cur.execute(
                        self.psycopg.sql.SQL("DROP TABLE IF EXISTS {}.{} CASCADE").format(
                            self.psycopg.sql.Identifier(self.schema),
                            self.psycopg.sql.Identifier(name),
                        )
                    )
                for spec in specs:
                    definitions = []
                    for col in spec["columns"]:
                        pg_type = safe_postgres_type(col["pg_type"])
                        ddl = self.psycopg.sql.SQL("{} {}{}{}").format(
                            self.psycopg.sql.Identifier(col["name"]),
                            self.psycopg.sql.SQL(pg_type),
                            self.psycopg.sql.SQL(
                                " GENERATED BY DEFAULT AS IDENTITY" if col.get("auto_increment") else ""
                            ),
                            self.psycopg.sql.SQL("" if col["nullable"] else " NOT NULL"),
                        )
                        definitions.append(ddl)
                    pk = spec.get("primary_key", [])
                    if pk:
                        definitions.append(
                            self.psycopg.sql.SQL("PRIMARY KEY ({})").format(
                                self.psycopg.sql.SQL(", ").join(
                                    self.psycopg.sql.Identifier(part) for part in pk
                                )
                            )
                        )
                    cur.execute(
                        self.psycopg.sql.SQL("CREATE TABLE {}.{} ({})").format(
                            self.psycopg.sql.Identifier(self.schema),
                            self.psycopg.sql.Identifier(spec["name"]),
                            self.psycopg.sql.SQL(", ").join(definitions),
                        )
                    )
            yield
            for spec in specs:
                for col in spec["columns"]:
                    if not col.get("auto_increment"):
                        continue
                    qualified = f"{quote_identifier(self.schema)}.{quote_identifier(spec['name'])}"
                    with self.connection.cursor() as cur:
                        cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (qualified, col["name"]))
                        sequence = cur.fetchone()[0]
                        if sequence:
                            cur.execute(
                                f"SELECT MAX({quote_identifier(col['name'])}) FROM {qualified}"
                            )
                            maximum = cur.fetchone()[0]
                            if maximum is not None:
                                cur.execute("SELECT setval(%s::regclass, %s, true)", (sequence, maximum))

    def insert_rows(self, spec: dict[str, Any], rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        fields = spec["columns"]
        query = self.psycopg.sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
            self.psycopg.sql.Identifier(self.schema),
            self.psycopg.sql.Identifier(spec["name"]),
            self.psycopg.sql.SQL(", ").join(
                self.psycopg.sql.Identifier(col["name"]) for col in fields
            ),
            self.psycopg.sql.SQL(", ").join(self.psycopg.sql.Placeholder() for _ in fields),
        )
        with self.connection.cursor() as cur:
            cur.executemany(query, rows)
