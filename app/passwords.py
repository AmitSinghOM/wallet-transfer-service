"""Bounded, off-loop Argon2id password work."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError


class PasswordCapacityError(Exception):
    pass


class PasswordService:
    MAX_JOBS = 2

    def __init__(self):
        self._hasher = PasswordHasher(
            time_cost=2,
            memory_cost=19 * 1024,
            parallelism=1,
            salt_len=16,
            hash_len=32,
            type=Type.ID,
            encoding="utf-8",
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self.MAX_JOBS, thread_name_prefix="password"
        )
        self._capacity = BoundedSemaphore(self.MAX_JOBS)
        self._closed = False

    async def _run(self, operation, *args):
        if self._closed or not self._capacity.acquire(blocking=False):
            raise PasswordCapacityError

        def work():
            try:
                return operation(*args)
            finally:
                # Cancellation of the HTTP task must not free a running job's slot.
                self._capacity.release()

        try:
            future = self._executor.submit(work)
        except Exception:
            self._capacity.release()
            raise
        pending = asyncio.wrap_future(future)
        pending.add_done_callback(lambda done: done.exception())
        return await asyncio.shield(pending)

    async def hash(self, password: str) -> str:
        return await self._run(self._hasher.hash, password)

    async def verify(
        self, password: str, password_hash: str
    ) -> tuple[bool, str | None]:
        return await self._run(self._verify, password, password_hash)

    def _verify(self, password: str, password_hash: str) -> tuple[bool, str | None]:
        try:
            if len(password_hash) > 512:
                return False, None
            params = extract_parameters(password_hash)
            # Stored PHC costs must not bypass this process's resource budget.
            if not (
                params.type == Type.ID
                and params.version == 19
                and 8 <= params.memory_cost <= self._hasher.memory_cost
                and 1 <= params.time_cost <= self._hasher.time_cost
                and params.parallelism == self._hasher.parallelism
                and 8 <= params.salt_len <= self._hasher.salt_len
                and 4 <= params.hash_len <= self._hasher.hash_len
            ):
                return False, None
            self._hasher.verify(password_hash, password)
        except (InvalidHashError, VerificationError, UnicodeError):
            return False, None
        replacement = None
        if self._hasher.check_needs_rehash(password_hash):
            replacement = self._hasher.hash(password)
        return True, replacement

    async def close(self):
        self._closed = True
        await asyncio.to_thread(self._executor.shutdown, wait=True)
