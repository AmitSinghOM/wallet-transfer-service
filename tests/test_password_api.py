"""Password policy, storage, overload and conditional upgrades against Postgres."""

import asyncio
import uuid
from threading import Event

import pytest
from argon2 import PasswordHasher

from app.main import app

pytestmark = pytest.mark.asyncio
PASSWORD = "correct-horse-99"
BAD_CREDENTIALS = {
    "code": "BAD_CREDENTIALS",
    "message": "Unknown username or wrong password",
}


def credentials(password=PASSWORD):
    return {"username": f"password_{uuid.uuid4().hex}", "password": password}


async def stored_hash(username):
    return await app.state.pool.fetchval(
        "SELECT password_hash FROM users WHERE username = $1", username
    )


@pytest.mark.parametrize("password", ["x" * 15, "x" * 128, "界" * 128, "🔐" * 128])
async def test_policy_boundaries_register_login_and_storage(client, password):
    body = credentials(password)
    registered = await client.post("/auth/register", json=body)
    assert registered.status_code == 201, registered.text
    encoded = await stored_hash(body["username"])
    assert encoded.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    login = await client.post("/auth/login", json=body)
    assert login.status_code == 200
    assert login.json()["user_id"] == registered.json()["user_id"]
    assert await stored_hash(body["username"]) == encoded


@pytest.mark.parametrize("password", ["", "x" * 14, "x" * 129, "界" * 129, None, 123])
@pytest.mark.parametrize("endpoint", ["register", "login"])
async def test_invalid_passwords_rejected_before_hash_or_database_write(
    client, monkeypatch, password, endpoint
):
    def unexpected(*args):
        pytest.fail("invalid input reached password work")

    monkeypatch.setattr(PasswordHasher, "hash", unexpected)
    monkeypatch.setattr(PasswordHasher, "verify", unexpected)
    body = credentials(password)
    response = await client.post(f"/auth/{endpoint}", json=body)
    assert response.status_code == 422
    assert await stored_hash(body["username"]) is None


@pytest.mark.parametrize("prefix", ["x" * 72, "界" * 24])
async def test_long_password_suffix_is_not_interchangeable(client, prefix):
    body = credentials(prefix + "suffix-A")
    assert (await client.post("/auth/register", json=body)).status_code == 201
    bad = await client.post(
        "/auth/login", json={**body, "password": prefix + "suffix-B"}
    )
    assert bad.status_code == 401 and bad.json()["detail"] == BAD_CREDENTIALS
    assert (await client.post("/auth/login", json=body)).status_code == 200


@pytest.mark.parametrize("encoded", [None, "malformed", "$2b$12$" + "x" * 53])
async def test_unknown_user_and_unsupported_hashes_have_generic_failure(
    client, encoded
):
    body = credentials()
    if encoded is not None:
        await app.state.pool.execute(
            "INSERT INTO users (username, password_hash) VALUES ($1, $2)",
            body["username"],
            encoded,
        )
    response = await client.post("/auth/login", json=body)
    assert response.status_code == 401
    assert response.json() == {"detail": BAD_CREDENTIALS}
    assert await stored_hash(body["username"]) == encoded


async def insert_old_hash(body):
    encoded = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(
        PASSWORD
    )
    await app.state.pool.execute(
        "INSERT INTO users (username, password_hash) VALUES ($1, $2)",
        body["username"],
        encoded,
    )
    return encoded


async def test_rehash_only_after_successful_verification(client):
    body = credentials()
    old = await insert_old_hash(body)
    wrong = await client.post(
        "/auth/login", json={**body, "password": "wrong-password-9"}
    )
    assert wrong.status_code == 401 and wrong.json()["detail"] == BAD_CREDENTIALS
    assert await stored_hash(body["username"]) == old
    response = await client.post("/auth/login", json=body)
    assert response.status_code == 200
    updated = await stored_hash(body["username"])
    assert updated != old
    assert await app.state.passwords.verify(PASSWORD, updated) == (True, None)


async def test_rehash_does_not_overwrite_concurrent_password_change(
    client, monkeypatch
):
    body = credentials()
    await insert_old_hash(body)
    new_password = "concurrently-changed-password"
    changed = await app.state.passwords.hash(new_password)
    verify = app.state.passwords.verify

    async def verify_then_change(*args):
        result = await verify(*args)
        assert result[0] and result[1] is not None
        await app.state.pool.execute(
            "UPDATE users SET password_hash = $1 WHERE username = $2",
            changed,
            body["username"],
        )
        return result

    with monkeypatch.context() as patch:
        patch.setattr(app.state.passwords, "verify", verify_then_change)
        response = await client.post("/auth/login", json=body)
    assert response.status_code == 401
    assert response.json() == {"detail": BAD_CREDENTIALS}
    assert await stored_hash(body["username"]) == changed
    response = await client.post("/auth/login", json={**body, "password": new_password})
    assert response.status_code == 200


async def test_two_concurrent_upgrades_do_not_overwrite_winner(client, monkeypatch):
    body = credentials()
    old = await insert_old_hash(body)
    verify = app.state.passwords.verify
    both_verified = asyncio.Event()
    replacements = []

    async def synchronize_verification(*args):
        result = await verify(*args)
        replacements.append(result[1])
        if len(replacements) == 2:
            both_verified.set()
        await asyncio.wait_for(both_verified.wait(), 2)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(app.state.passwords, "verify", synchronize_verification)
        responses = await asyncio.gather(
            client.post("/auth/login", json=body), client.post("/auth/login", json=body)
        )
    assert sorted(r.status_code for r in responses) == [200, 401]
    updated = await stored_hash(body["username"])
    assert updated in replacements and updated != old
    assert (await client.post("/auth/login", json=body)).status_code == 200


async def test_overload_and_cancellation_keep_health_responsive_without_writes(
    client, monkeypatch
):
    login_body = credentials()
    assert (await client.post("/auth/register", json=login_body)).status_code == 201
    original_hash = await stored_hash(login_body["username"])
    loop = asyncio.get_running_loop()
    entered = [asyncio.Event(), asyncio.Event()]
    release = Event()
    bodies = [credentials(), credentials()]
    started = 0
    original = PasswordHasher.hash

    def blocked(hasher, password, *args, **kwargs):
        nonlocal started
        index = started
        started += 1
        loop.call_soon_threadsafe(entered[index].set)
        assert release.wait(5), "test failed to release native hashing"
        return original(hasher, password, *args, **kwargs)

    monkeypatch.setattr(PasswordHasher, "hash", blocked)
    tasks = []
    try:
        for index, body in enumerate(bodies):
            tasks.append(asyncio.create_task(client.post("/auth/register", json=body)))
            await asyncio.wait_for(entered[index].wait(), 2)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        overloaded = credentials()
        for endpoint, body in (("register", overloaded), ("login", login_body)):
            response = await asyncio.wait_for(
                client.post(f"/auth/{endpoint}", json=body), 2
            )
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "AUTH_BUSY"
            assert response.headers["Retry-After"] == "2"
        for path in ("/healthz", "/readyz"):
            response = await asyncio.wait_for(client.get(path), 2)
            assert response.status_code == 200
        assert await stored_hash(overloaded["username"]) is None
        assert await stored_hash(login_body["username"]) == original_hash
        assert started == 2
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert await stored_hash(bodies[0]["username"]) is None
    assert tasks[1].result().status_code == 201
