"""Password hashing and native-job admission, without a database."""

import asyncio
from threading import Event

import pytest
import pytest_asyncio
from argon2 import PasswordHasher, Type, extract_parameters

from app.passwords import PasswordCapacityError, PasswordService

pytestmark = pytest.mark.asyncio
PASSWORD = "correct-horse-99"


@pytest_asyncio.fixture
async def passwords():
    service = PasswordService()
    try:
        yield service
    finally:
        await service.close()


async def test_phc_parameters_random_salt_and_no_unnecessary_rehash(passwords):
    first = await passwords.hash(PASSWORD)
    second = await passwords.hash(PASSWORD)
    assert first != second
    params = extract_parameters(first)
    assert params.type == Type.ID
    assert (params.version, params.memory_cost, params.time_cost) == (19, 19456, 2)
    assert (params.parallelism, params.salt_len, params.hash_len) == (1, 16, 32)
    assert await passwords.verify(PASSWORD, first) == (True, None)
    assert await passwords.verify("wrong-password-9", first) == (False, None)


@pytest.mark.parametrize("prefix", ["a" * 72, "界" * 24, "🔐" * 18])
async def test_bytes_after_72_are_significant(passwords, prefix):
    encoded = await passwords.hash(prefix + "suffix-A")
    assert await passwords.verify(prefix + "suffix-A", encoded) == (True, None)
    assert await passwords.verify(prefix + "suffix-B", encoded) == (False, None)


async def test_no_trimming_or_unicode_normalization(passwords):
    password = "  café-password-99  "
    encoded = await passwords.hash(password)
    assert await passwords.verify(password, encoded) == (True, None)
    assert await passwords.verify(password.strip(), encoded) == (False, None)
    assert await passwords.verify(password.replace("é", "e\u0301"), encoded) == (
        False,
        None,
    )


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "x" * 513,
        "$2b$12$" + "x" * 53,
        "$argon2id$v=19$m=19456,t=2,p=1$invalid$invalid",
        "$argon2id$v=19$m=19456,t=2,p=1$" + "é" * 22 + "$" + "x" * 43,
    ],
)
async def test_malformed_and_legacy_hashes_fail_closed(passwords, encoded):
    assert await passwords.verify(PASSWORD, encoded) == (False, None)


@pytest.mark.parametrize(
    "old,new",
    [
        ("argon2id", "argon2i"),
        ("v=19", "v=18"),
        ("m=19456", "m=19457"),
        ("m=19456", "m=0"),
        ("t=2", "t=3"),
        ("t=2", "t=0"),
        ("p=1", "p=2"),
    ],
)
async def test_unsupported_costs_never_reach_native_verification(
    passwords, monkeypatch, old, new
):
    encoded = (await passwords.hash(PASSWORD)).replace(old, new)

    def unexpected(*args):
        pytest.fail("unsupported PHC cost reached native verification")

    monkeypatch.setattr(PasswordHasher, "verify", unexpected)
    assert await passwords.verify(PASSWORD, encoded) == (False, None)


async def test_successful_old_parameters_rehashed_but_wrong_password_is_not(passwords):
    old = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
    assert await passwords.verify("wrong-password-9", old) == (False, None)
    valid, replacement = await passwords.verify(PASSWORD, old)
    assert valid and replacement != old
    assert await passwords.verify(PASSWORD, replacement) == (True, None)


async def test_shared_capacity_survives_cancellation_and_drains_on_close(
    passwords, monkeypatch
):
    loop = asyncio.get_running_loop()
    entered = [asyncio.Event(), asyncio.Event()]
    release = Event()
    native_threads = []

    def blocked(index):
        from threading import get_ident

        native_threads.append(get_ident())
        loop.call_soon_threadsafe(entered[index].set)
        assert release.wait(5), "test failed to release native job"
        return "hash" if index == 0 else (True, None)

    monkeypatch.setattr(PasswordHasher, "hash", lambda *args: blocked(0))
    monkeypatch.setattr(passwords, "_verify", lambda *args: blocked(1))
    first = asyncio.create_task(passwords.hash(PASSWORD))
    second = asyncio.create_task(passwords.verify(PASSWORD, "stored"))
    closing = None
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered)), 2)
        assert len(set(native_threads)) == 2
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        for operation in (
            passwords.hash(PASSWORD),
            passwords.verify(PASSWORD, "stored"),
        ):
            with pytest.raises(PasswordCapacityError):
                await operation
        closing = asyncio.create_task(passwords.close())
        started_close = asyncio.Event()
        loop.call_soon(started_close.set)
        await started_close.wait()
        assert not closing.done(), "shutdown returned while native jobs still ran"
        with pytest.raises(PasswordCapacityError):
            await passwords.hash(PASSWORD)
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        if closing is not None:
            await closing
    assert second.result() == (True, None)


async def test_worker_exception_and_submit_failure_release_capacity(
    passwords, monkeypatch
):
    def fail(*args):
        raise RuntimeError("synthetic worker failure")

    with monkeypatch.context() as patch:
        patch.setattr(PasswordHasher, "hash", fail)
        for _ in range(3):
            with pytest.raises(RuntimeError, match="synthetic worker failure"):
                await passwords.hash(PASSWORD)
    with monkeypatch.context() as patch:
        patch.setattr(passwords._executor, "submit", fail)
        for _ in range(3):
            with pytest.raises(RuntimeError, match="synthetic worker failure"):
                await passwords.hash(PASSWORD)
    assert await passwords.hash(PASSWORD)


async def test_cancelled_worker_failure_is_observed_and_capacity_reusable(passwords):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = Event()
    completed = asyncio.Event()
    errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))

    def fail_after_release():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        raise RuntimeError("cancelled native job failed")

    task = asyncio.create_task(passwords._run(fail_after_release))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await passwords.close()
        loop.call_soon(completed.set)
        await completed.wait()
        assert not errors
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        loop.set_exception_handler(previous_handler)
