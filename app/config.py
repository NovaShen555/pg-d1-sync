from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Optional


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, *aliases: str, default: Optional[str] = None) -> Optional[str]:
    for key in (name, *aliases):
        value = os.getenv(key)
        if value is not None and value.strip():
            return value.strip()
    return default


def _required(name: str, *aliases: str) -> str:
    value = _env(name, *aliases)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _number(name: str, default: str) -> float:
    try:
        value = float(_env(name, default=default) or default)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return value


def _integer(name: str, default: str, maximum: int) -> int:
    try:
        value = int(_env(name, default=default) or default)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise ConfigError(f"{name} must be between 1 and {maximum}")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    value = (_env(name, default="true" if default else "false") or "false").lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    postgres_dsn: str
    cloudflare_api_token: str
    cloudflare_account_id: str
    d1_database_id: str
    mode: str = "push"
    postgres_schema: str = "public"
    sync_tables: tuple[str, ...] = ()
    sync_interval_seconds: float = 300.0
    retry_delay_seconds: float = 30.0
    d1_batch_size: int = 20
    d1_page_size: int = 100
    d1_request_timeout_seconds: float = 30.0
    allow_destructive_pull: bool = False
    once: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env(
        cls,
        *,
        mode_override: Optional[str] = None,
        once_override: Optional[bool] = None,
    ) -> "Settings":
        mode = (mode_override or _env("SYNC_MODE", default="push") or "push").lower()
        if mode not in {"push", "pull"}:
            raise ConfigError("SYNC_MODE must be either 'push' or 'pull'")
        schema = _env("POSTGRES_SCHEMA", default="public") or "public"
        if not schema or "\x00" in schema:
            raise ConfigError("POSTGRES_SCHEMA is invalid")
        raw_tables = _env("SYNC_TABLES", default="") or ""
        tables = tuple(dict.fromkeys(part.strip() for part in raw_tables.split(",") if part.strip()))
        if any("\x00" in table for table in tables):
            raise ConfigError("SYNC_TABLES contains an invalid name")
        return cls(
            postgres_dsn=_required("POSTGRES_DSN", "DATABASE_URL"),
            cloudflare_api_token=_required("CLOUDFLARE_API_TOKEN", "CF_API_TOKEN"),
            cloudflare_account_id=_required("CLOUDFLARE_ACCOUNT_ID", "CF_ACCOUNT_ID"),
            d1_database_id=_required("D1_DATABASE_ID", "CF_D1_DATABASE_ID"),
            mode=mode,
            postgres_schema=schema,
            sync_tables=tables,
            sync_interval_seconds=_number("SYNC_INTERVAL_SECONDS", "300"),
            retry_delay_seconds=_number("RETRY_DELAY_SECONDS", "30"),
            d1_batch_size=_integer("D1_BATCH_SIZE", "20", 50),
            d1_page_size=_integer("D1_PAGE_SIZE", "100", 500),
            d1_request_timeout_seconds=_number("D1_REQUEST_TIMEOUT_SECONDS", "30"),
            allow_destructive_pull=_boolean("ALLOW_DESTRUCTIVE_PULL"),
            once=once_override if once_override is not None else _boolean("SYNC_ONCE"),
            log_level=(_env("LOG_LEVEL", default="INFO") or "INFO").upper(),
        )
