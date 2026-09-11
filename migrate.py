"""Forward-only SQL migration runner.

Applies migrations/*.sql in filename order inside a single transaction,
recording applied filenames in schema_migrations. Safe to run repeatedly
(and concurrently: pg_advisory_xact_lock serializes racing runners, e.g.
two app replicas starting at once).

Usage: python migrate.py  (reads DATABASE_URL from the environment)
"""

import asyncio
import os
import pathlib
import sys

import asyncpg

from app.config import _normalize_dsn

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"
LOCK_KEY = 74_1001  # arbitrary app-wide advisory lock id for migrations


async def migrate() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(_normalize_dsn(dsn))
    applied_count = 0
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       filename   TEXT PRIMARY KEY,
                       applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                   )"""
            )
            done = {
                r["filename"]
                for r in await conn.fetch("SELECT filename FROM schema_migrations")
            }
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                await conn.execute(path.read_text())
                await conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES ($1)", path.name
                )
                print(f"applied {path.name}")
                applied_count += 1
    finally:
        await conn.close()
    print(f"migrations complete ({applied_count} applied)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(migrate()))
