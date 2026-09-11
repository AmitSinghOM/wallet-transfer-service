"""Functional tests: auth, accounts, transfers, edge cases."""

import uuid

import pytest

from .conftest import auth, register

pytestmark = pytest.mark.asyncio

GRANT = 100_000  # WELCOME_GRANT_PAISE default


# ---------------------------------------------------------------- auth


async def test_register_login_and_duplicate(client):
    u = await register(client, username="alice_test_1")
    assert u["token"]

    r = await client.post(
        "/auth/register",
        json={"username": "alice_test_1", "password": "another-pass-9"},
    )
    assert r.status_code == 409

    r = await client.post(
        "/auth/login",
        json={"username": "alice_test_1", "password": "correct-horse-9"},
    )
    assert r.status_code == 200 and r.json()["user_id"] == u["user_id"]

    r = await client.post(
        "/auth/login",
        json={"username": "alice_test_1", "password": "wrong-password-9"},
    )
    assert r.status_code == 401


async def test_requests_without_token_are_rejected(client):
    for method, path in [
        ("POST", "/accounts"),
        ("GET", "/accounts/me"),
        ("POST", "/transfers"),
        ("GET", f"/transfers/{uuid.uuid4()}"),
    ]:
        r = await client.request(method, path, json={})
        assert r.status_code == 401, (method, path, r.status_code)


async def test_garbage_token_rejected(client):
    r = await client.get("/accounts/me", headers=auth("not.a.jwt"))
    assert r.status_code == 401


# ------------------------------------------------------------- accounts


async def test_account_get_or_create_is_idempotent(client):
    u = await register(client)
    r1 = await client.post("/accounts", headers=auth(u["token"]))
    r2 = await client.post("/accounts", headers=auth(u["token"]))
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert r1.json()["balance_paise"] == GRANT
    assert r1.json()["balance"] == GRANT  # brief's literal field name

    r = await client.get("/accounts/me", headers=auth(u["token"]))
    assert r.json()["balance_paise"] == GRANT
    assert r.json()["balance"] == GRANT


async def test_me_without_wallet_404(client):
    u = await register(client)
    r = await client.get("/accounts/me", headers=auth(u["token"]))
    assert r.status_code == 404


# ------------------------------------------------------------ transfers


async def _setup_pair(client):
    a, b = await register(client), await register(client)
    await client.post("/accounts", headers=auth(a["token"]))
    return a, b


async def test_happy_path_transfer_and_read(client):
    a, b = await _setup_pair(client)
    r = await client.post(
        "/transfers",
        headers=auth(a["token"]),
        json={
            "to_user": b["user_id"],
            "amount_paise": 25_000,
            "idempotency_key": "k-happy-1",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["new_balance"] == GRANT - 25_000

    # Recipient wallet was created as part of the transfer, with grant + amount.
    r = await client.get("/accounts/me", headers=auth(b["token"]))
    assert r.json()["balance_paise"] == GRANT + 25_000

    # Both participants can read the transfer; a third party gets 404.
    for who in (a, b):
        r = await client.get(
            f"/transfers/{body['transfer_id']}", headers=auth(who["token"])
        )
        assert r.status_code == 200
        assert r.json()["amount_paise"] == 25_000
    outsider = await register(client)
    r = await client.get(
        f"/transfers/{body['transfer_id']}", headers=auth(outsider["token"])
    )
    assert r.status_code == 404


async def test_insufficient_funds_402_and_replayed(client):
    a, b = await _setup_pair(client)
    payload = {
        "to_user": b["user_id"],
        "amount_paise": GRANT + 1,
        "idempotency_key": "k-poor-1",
    }
    r1 = await client.post("/transfers", headers=auth(a["token"]), json=payload)
    assert r1.status_code == 402
    # Retry with the same key replays the SAME rejection (no re-execution).
    r2 = await client.post("/transfers", headers=auth(a["token"]), json=payload)
    assert r2.status_code == 402
    assert r2.headers.get("X-Idempotent-Replay") == "true"
    assert r1.json()["detail"]["transfer_id"] == r2.json()["detail"]["transfer_id"]
    # No money moved.
    r = await client.get("/accounts/me", headers=auth(a["token"]))
    assert r.json()["balance_paise"] == GRANT


async def test_idempotent_replay_and_conflict(client):
    a, b = await _setup_pair(client)
    payload = {
        "to_user": b["user_id"],
        "amount_paise": 10_000,
        "idempotency_key": "k-replay-1",
    }
    r1 = await client.post("/transfers", headers=auth(a["token"]), json=payload)
    r2 = await client.post("/transfers", headers=auth(a["token"]), json=payload)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()  # same transfer_id, same new_balance
    assert r2.headers.get("X-Idempotent-Replay") == "true"

    # Same key + different body -> 409, and nothing moved.
    r3 = await client.post(
        "/transfers",
        headers=auth(a["token"]),
        json={**payload, "amount_paise": 99},
    )
    assert r3.status_code == 409
    r = await client.get("/accounts/me", headers=auth(a["token"]))
    assert r.json()["balance_paise"] == GRANT - 10_000

    # The same key from a DIFFERENT caller is a different scope: no clash.
    await client.post("/accounts", headers=auth(b["token"]))
    r4 = await client.post(
        "/transfers",
        headers=auth(b["token"]),
        json={
            "to_user": a["user_id"],
            "amount_paise": 5_000,
            "idempotency_key": "k-replay-1",
        },
    )
    assert r4.status_code == 200


async def test_edge_cases(client):
    a, _ = await _setup_pair(client)

    # self-transfer
    r = await client.post(
        "/transfers",
        headers=auth(a["token"]),
        json={
            "to_user": a["user_id"],
            "amount_paise": 100,
            "idempotency_key": "k-self",
        },
    )
    assert r.status_code == 422

    # zero and negative amounts (validation)
    for amount in (0, -5):
        r = await client.post(
            "/transfers",
            headers=auth(a["token"]),
            json={
                "to_user": str(uuid.uuid4()),
                "amount_paise": amount,
                "idempotency_key": "k-bad",
            },
        )
        assert r.status_code == 422

    # unknown recipient
    r = await client.post(
        "/transfers",
        headers=auth(a["token"]),
        json={
            "to_user": str(uuid.uuid4()),
            "amount_paise": 100,
            "idempotency_key": "k-ghost",
        },
    )
    assert r.status_code == 404


async def test_health_endpoints(client):
    r = await client.get("/")
    assert r.status_code == 200 and r.json()["docs"] == "/docs"
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 200
    m = await client.get("/metrics")
    assert m.status_code == 200 and b"http_requests_total" in m.content
