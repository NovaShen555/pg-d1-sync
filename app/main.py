from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from .config import ConfigError, Settings
from .d1_client import D1Client
from .postgres_db import PostgresDatabase
from .sync_engine import SyncEngine

LOGGER = logging.getLogger("pg_d1_sync")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PostgreSQL -> D1 mirror and one-shot restore")
    parser.add_argument("--mode", choices=("push", "pull"), help="override SYNC_MODE")
    parser.add_argument("--once", action="store_true", help="run one push and exit")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    try:
        settings = Settings.from_env(
            mode_override=args.mode,
            once_override=True if args.once else None,
        )
        if settings.mode == "pull" and not settings.allow_destructive_pull:
            raise ConfigError("Set ALLOW_DESTRUCTIVE_PULL=true to confirm local table replacement")
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stopping = False

    def stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        LOGGER.info("Signal %s received; stopping after current operation", signum)
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        pg = PostgresDatabase(settings.postgres_dsn, settings.postgres_schema)
        pg.acquire_lock()
    except Exception:
        LOGGER.exception("PostgreSQL connection or lock failed")
        return 1
    d1 = D1Client(
        account_id=settings.cloudflare_account_id,
        database_id=settings.d1_database_id,
        api_token=settings.cloudflare_api_token,
        timeout_seconds=settings.d1_request_timeout_seconds,
    )
    engine = SyncEngine(settings, pg, d1)
    try:
        if settings.mode == "pull":
            try:
                engine.pull_once()
                return 0
            except Exception:
                LOGGER.exception("One-shot pull failed; exiting without retry")
                return 1

        while not stopping:
            start = time.monotonic()
            try:
                engine.push_once()
                if settings.once:
                    return 0
                delay = max(0.0, settings.sync_interval_seconds - (time.monotonic() - start))
            except Exception:
                LOGGER.exception("Push failed")
                if settings.once:
                    return 1
                delay = settings.retry_delay_seconds
            deadline = time.monotonic() + delay
            while not stopping and time.monotonic() < deadline:
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        return 0
    finally:
        pg.close()


if __name__ == "__main__":
    raise SystemExit(main())
