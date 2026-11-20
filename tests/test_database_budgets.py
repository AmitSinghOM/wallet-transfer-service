import asyncio
import uuid
from dataclasses import replace

import asyncpg
import pytest

from app import core
from app.config import load_settings
from app.datastore import Database
from app.main import app

from .conftest import auth, register

pytestmark = pytest.mark.asyncio


async def database(test_db_url, **overrides):
    settings = replace(
        load_settings(),
        database_url=test_db_url,
        db_pool_min=1,
        db_pool_max=1,
        db_acquire_timeout_seconds=0.05,
        db_operation_timeout_seconds=0.25,
        db_command_timeout_seconds=0.1,
        db_lock_timeout_seconds=0.05,
        **overrides,
    )
    return await Database.open(settings)


async def test_exhausted_pool_fails_then_recovers(test_db_url):
    db = await database(test_db_url)
    try:
        async with db.acquire():
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(db.fetchval("SELECT 1"), 1)
        assert await db.fetchval("SELECT 1") == 1
    finally:
        await db.close()


async def test_statement_timeout_and_total_operation_budget(test_db_url):
    db = await database(test_db_url)
    try:
        with pytest.raises((TimeoutError, asyncpg.QueryCanceledError)):
            await asyncio.wait_for(db.fetchval("SELECT pg_sleep(5)"), 1)
        with pytest.raises(TimeoutError):
            async with db.acquire() as conn:
                for _ in range(10):
                    await conn.execute("SELECT pg_sleep(0.04)")
        assert await db.fetchval("SELECT 1") == 1
        assert await db.fetchval("SHOW lock_timeout") == "50ms"
    finally:
        await db.close()


async def test_blocked_transfer_returns_503_without_partial_write_and_retries(
    client, test_db_url, monkeypatch
):
    a, b = await register(client), await register(client)
    for user in (a, b):
        assert (
            await client.post("/accounts", headers=auth(user["token"]))
        ).status_code == 200
    db = await database(test_db_url)
    blocker = await asyncpg.connect(test_db_url)
    payload = {
        "to_user": b["user_id"],
        "amount_paise": 100,
        "idempotency_key": uuid.uuid4().hex,
    }
    try:
        async with blocker.transaction():
            await blocker.execute(
                "SELECT 1 FROM wallets WHERE user_id=$1 FOR UPDATE",
                uuid.UUID(a["user_id"]),
            )
            with monkeypatch.context() as patch:
                patch.setattr(app.state, "pool", db)
                response = await asyncio.wait_for(
                    client.post("/transfers", headers=auth(a["token"]), json=payload), 1
                )
            assert response.status_code == 503
            assert response.headers["Retry-After"] == "2"
            assert (
                await blocker.fetchval(
                    "SELECT count(*) FROM transfers WHERE idempotency_key=$1",
                    payload["idempotency_key"],
                )
                == 0
            )
        with monkeypatch.context() as patch:
            patch.setattr(app.state, "pool", db)
            first = await client.post(
                "/transfers", headers=auth(a["token"]), json=payload
            )
            replay = await client.post(
                "/transfers", headers=auth(a["token"]), json=payload
            )
        assert first.status_code == replay.status_code == 200
        assert first.json() == replay.json()
        assert replay.headers["X-Idempotent-Replay"] == "true"
    finally:
        await blocker.close()
        await db.close()


async def test_cancellation_rolls_back_and_releases_connection(test_db_url):
    db = await database(test_db_url)
    entered = asyncio.Event()
    username = "cancel_" + uuid.uuid4().hex

    async def transaction():
        async with db.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO users(username,password_hash) VALUES($1,'synthetic')",
                username,
            )
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(transaction())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (
            await db.fetchval("SELECT count(*) FROM users WHERE username=$1", username)
            == 0
        )
        assert await db.fetchval("SELECT 1") == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await db.close()


async def test_lost_response_after_commit_does_not_double_apply(client, monkeypatch):
    a, b = await register(client), await register(client)
    original = core.execute_transfer

    async def lose_response(*args, **kwargs):
        await original(*args, **kwargs)
        raise TimeoutError("synthetic lost acknowledgement")

    payload = {
        "to_user": b["user_id"],
        "amount_paise": 123,
        "idempotency_key": uuid.uuid4().hex,
    }
    with monkeypatch.context() as patch:
        patch.setattr(core, "execute_transfer", lose_response)
        lost = await client.post("/transfers", headers=auth(a["token"]), json=payload)
    assert lost.status_code == 503
    retry = await client.post("/transfers", headers=auth(a["token"]), json=payload)
    assert retry.status_code == 200
    assert retry.headers["X-Idempotent-Replay"] == "true"
    assert retry.json()["new_balance"] == 100000 - 123
    assert (
        await app.state.pool.fetchval(
            "SELECT count(*) FROM transfers WHERE idempotency_key=$1",
            payload["idempotency_key"],
        )
        == 1
    )


async def test_readiness_has_short_budget_without_hiding_programming_errors(
    client, monkeypatch
):
    class Blocked:
        async def fetchval(self, *args):
            await asyncio.Event().wait()

    monkeypatch.setattr(app.state, "pool", Blocked())
    monkeypatch.setattr(
        app.state,
        "settings",
        replace(app.state.settings, readiness_timeout_seconds=0.03),
    )
    response = await asyncio.wait_for(client.get("/readyz"), 1)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "NOT_READY"
    assert (await client.get("/healthz")).status_code == 200
