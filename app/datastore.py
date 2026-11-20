import asyncio
from contextlib import asynccontextmanager

import asyncpg

from .config import Settings

DATASTORE_ERRORS = (
    OSError,
    TimeoutError,
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.TooManyConnectionsError,
    asyncpg.QueryCanceledError,
    asyncpg.LockNotAvailableError,
)


class Database:
    def __init__(self, pool: asyncpg.Pool, settings: Settings):
        self._pool = pool
        self._settings = settings

    @classmethod
    async def open(cls, settings: Settings):
        pool = asyncpg.create_pool(
            settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            timeout=settings.db_connect_timeout_seconds,
            command_timeout=settings.db_command_timeout_seconds,
            server_settings={
                "statement_timeout": str(
                    int(settings.db_command_timeout_seconds * 1000)
                ),
                "lock_timeout": str(int(settings.db_lock_timeout_seconds * 1000)),
                "idle_in_transaction_session_timeout": str(
                    int(settings.db_operation_timeout_seconds * 1000)
                ),
            },
        )
        try:
            async with asyncio.timeout(settings.db_connect_timeout_seconds):
                await pool
        except BaseException:
            pool.terminate()
            raise
        return cls(pool, settings)

    @asynccontextmanager
    async def acquire(self):
        # The deadline covers the entire checkout, including multi-statement transfers.
        async with asyncio.timeout(self._settings.db_operation_timeout_seconds):
            async with self._pool.acquire(
                timeout=self._settings.db_acquire_timeout_seconds
            ) as conn:
                yield conn

    async def fetchval(self, query, *args):
        async with self.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def fetchrow(self, query, *args):
        async with self.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def execute(self, query, *args):
        async with self.acquire() as conn:
            return await conn.execute(query, *args)

    async def close(self):
        try:
            async with asyncio.timeout(self._settings.db_operation_timeout_seconds):
                await self._pool.close()
        except BaseException:
            self._pool.terminate()
            raise
