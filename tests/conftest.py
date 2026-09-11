"""Test fixtures.

Tests run against a REAL throwaway Postgres database (created and dropped
per session) — concurrency behavior can't be proven against a fake.
Set TEST_ADMIN_DATABASE_URL to point at a reachable Postgres superuser/db;
defaults to the local dev instance.
"""

import os
import secrets

import asyncpg
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

ADMIN_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL",
    f"postgresql://{os.environ.get('USER')}@127.0.0.1:5432/postgres",
)


@pytest_asyncio.fixture(scope="session")
async def test_db_url():
    dbname = f"wallet_test_{secrets.token_hex(4)}"
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    await admin.close()

    url = ADMIN_URL.rsplit("/", 1)[0] + f"/{dbname}"
    os.environ["DATABASE_URL"] = url
    os.environ["APP_ENV"] = "development"
    os.environ.pop("JWT_SECRET", None)

    import migrate

    assert await migrate.migrate() == 0

    yield url

    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
    await admin.close()


@pytest_asyncio.fixture()
async def client(test_db_url):
    from app.main import app

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            yield c


async def register(client: AsyncClient, username: str | None = None) -> dict:
    """Register a fresh user; returns {user_id, token}."""
    username = username or f"user_{secrets.token_hex(6)}"
    r = await client.post(
        "/auth/register",
        json={"username": username, "password": "correct-horse-9"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
