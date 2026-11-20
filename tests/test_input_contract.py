"""Input contract at the trust boundary: which JWTs identify a caller and
which transfer amounts reach the money path. Every rejection must be the
same generic response and must leave no trace in the database."""

import time
import uuid

import asyncpg
import jwt
import pytest

from .conftest import auth, register

pytestmark = pytest.mark.asyncio

GRANT = 100_000
AMOUNT_LIMIT = 10_00_00_00_000
DEV_SECRET = "dev-only-secret-do-not-use-in-prod"
GENERIC_401 = {"code": "UNAUTHORIZED", "message": "Invalid or missing credentials"}


_DROP = object()


def _tok(claims: dict, secret: str = DEV_SECRET) -> str:
    return jwt.encode(claims, secret, algorithm="HS256")


def _claims(subject: str, **overrides) -> dict:
    now = int(time.time())
    claims = {"sub": subject, "iat": now, "exp": now + 600}
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not _DROP}


async def _assert_rejected_without_wallet(client, user, token):
    r = await client.post("/accounts", headers=auth(token))
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == GENERIC_401
    assert r.headers["WWW-Authenticate"] == "Bearer"
    r = await client.get("/accounts/me", headers=auth(user["token"]))
    assert r.status_code == 404, "a rejected token must not create a wallet"


# ------------------------------------------------------------------- tokens


async def test_issued_token_carries_integer_claims_and_is_accepted(client):
    u = await register(client)
    claims = jwt.decode(u["token"], DEV_SECRET, algorithms=["HS256"])
    assert claims["sub"] == u["user_id"]
    assert type(claims["iat"]) is int and type(claims["exp"]) is int
    assert claims["exp"] > claims["iat"]
    r = await client.post("/accounts", headers=auth(u["token"]))
    assert r.status_code == 200


async def test_handcrafted_well_formed_token_is_accepted(client):
    u = await register(client)
    r = await client.post("/accounts", headers=auth(_tok(_claims(u["user_id"]))))
    assert r.status_code == 200


@pytest.mark.parametrize("claim", ["sub", "exp", "iat"])
async def test_token_missing_required_claim_401(client, claim):
    u = await register(client)
    token = _tok(_claims(u["user_id"], **{claim: _DROP}))
    await _assert_rejected_without_wallet(client, u, token)


MALFORMED_NUMERIC_DATES = [
    pytest.param(None, id="null"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(str(int(time.time()) + 600), id="numeric-string"),
    pytest.param("soon", id="string"),
    pytest.param(int(time.time()) + 600.5, id="float"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="-inf"),
    pytest.param([1], id="list"),
    pytest.param({}, id="object"),
]


@pytest.mark.parametrize("value", MALFORMED_NUMERIC_DATES)
@pytest.mark.parametrize("claim", ["exp", "iat", "nbf"])
async def test_token_malformed_numeric_date_401(client, claim, value):
    u = await register(client)
    token = _tok(_claims(u["user_id"], **{claim: value}))
    await _assert_rejected_without_wallet(client, u, token)


async def test_expired_token_401(client):
    u = await register(client)
    now = int(time.time())
    token = _tok(_claims(u["user_id"], iat=now - 7200, exp=now - 1))
    await _assert_rejected_without_wallet(client, u, token)


@pytest.mark.parametrize("claim", ["iat", "nbf"])
async def test_future_iat_or_nbf_401(client, claim):
    u = await register(client)
    token = _tok(_claims(u["user_id"], **{claim: int(time.time()) + 300}))
    await _assert_rejected_without_wallet(client, u, token)


@pytest.mark.parametrize("age", [0, 60], ids=["nbf-now", "nbf-past"])
async def test_integer_nbf_not_in_future_is_accepted(client, age):
    u = await register(client)
    token = _tok(_claims(u["user_id"], nbf=int(time.time()) - age))
    r = await client.post("/accounts", headers=auth(token))
    assert r.status_code == 200, r.text


async def test_bad_signature_401(client):
    u = await register(client)
    token = _tok(_claims(u["user_id"]), secret="not-the-server-secret-for-test-only")
    await _assert_rejected_without_wallet(client, u, token)


# ------------------------------------------------------------------ amounts


async def _funded_pair(client):
    a, b = await register(client), await register(client)
    r = await client.post("/accounts", headers=auth(a["token"]))
    assert r.json()["balance_paise"] == GRANT
    return a, b


@pytest.mark.parametrize(
    "amount",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param("100", id="numeric-string"),
        pytest.param("1e2", id="exponent-string"),
        pytest.param(100.0, id="integral-float"),
        pytest.param(100.5, id="fractional-float"),
        pytest.param(None, id="null"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(AMOUNT_LIMIT + 1, id="limit-plus-one"),
        pytest.param(2**63, id="bigint-overflow"),
        pytest.param(10**30, id="huge"),
    ],
)
async def test_invalid_amount_422_without_side_effects(client, test_db_url, amount):
    a, b = await _funded_pair(client)
    key = f"k-invalid-{uuid.uuid4()}"
    r = await client.post(
        "/transfers",
        headers=auth(a["token"]),
        json={"to_user": b["user_id"], "amount_paise": amount, "idempotency_key": key},
    )
    assert r.status_code == 422, r.text

    r = await client.get("/accounts/me", headers=auth(a["token"]))
    assert r.json()["balance_paise"] == GRANT
    r = await client.get("/accounts/me", headers=auth(b["token"]))
    assert r.status_code == 404, "recipient wallet must not be created"
    conn = await asyncpg.connect(test_db_url)
    try:
        rows = await conn.fetchval(
            "SELECT count(*) FROM transfers WHERE from_user = $1",
            uuid.UUID(a["user_id"]),
        )
    finally:
        await conn.close()
    assert rows == 0

    # The key was never claimed: a valid request with it is a fresh transfer.
    r = await client.post(
        "/transfers",
        headers=auth(a["token"]),
        json={"to_user": b["user_id"], "amount_paise": 1, "idempotency_key": key},
    )
    assert r.status_code == 200, r.text
    assert "X-Idempotent-Replay" not in r.headers


@pytest.mark.parametrize(
    "amount,status,sender_after",
    [
        (1, 200, GRANT - 1),
        (GRANT, 200, 0),
        (AMOUNT_LIMIT, 402, GRANT),
    ],
)
async def test_valid_integer_amounts_reach_the_money_path(
    client, amount, status, sender_after
):
    a, b = await _funded_pair(client)
    r = await client.post(
        "/transfers",
        headers=auth(a["token"]),
        json={
            "to_user": b["user_id"],
            "amount_paise": amount,
            "idempotency_key": f"k-valid-{uuid.uuid4()}",
        },
    )
    assert r.status_code == status, r.text
    r = await client.get("/accounts/me", headers=auth(a["token"]))
    assert r.json()["balance_paise"] == sender_after
