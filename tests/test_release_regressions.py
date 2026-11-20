"""Readiness error contracts and JWT decoder upgrade regressions."""

import base64
import hashlib
import hmac
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.datastore import DATASTORE_ERRORS
from app.main import app

from .conftest import auth, register

pytestmark = pytest.mark.asyncio
SECRET = "dev-only-secret-do-not-use-in-prod"


def signed_token(header, payload):
    def encode(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    message = encode(json.dumps(header).encode()) + b"." + encode(payload)
    signature = hmac.new(SECRET.encode(), message, hashlib.sha256).digest()
    return (message + b"." + encode(signature)).decode()


def payload_for(user_id):
    now = int(time.time())
    return json.dumps({"sub": user_id, "iat": now, "exp": now + 600}).encode()


@pytest.mark.parametrize("error_type", DATASTORE_ERRORS)
async def test_readiness_expected_datastore_failures_remain_503(
    client, monkeypatch, error_type
):
    pool = SimpleNamespace(
        fetchval=AsyncMock(side_effect=error_type("synthetic outage"))
    )
    with monkeypatch.context() as patch:
        patch.setattr(app.state, "pool", pool)
        response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {
            "detail": {"code": "NOT_READY", "message": "datastore unreachable"}
        }
        assert (await client.get("/healthz")).status_code == 200
    pool.fetchval.assert_awaited_once_with("SELECT 1")


async def test_unexpected_readiness_failure_is_not_disguised_as_outage(
    client, monkeypatch
):
    pool = SimpleNamespace(
        fetchval=AsyncMock(side_effect=ValueError("synthetic defect"))
    )
    with monkeypatch.context() as patch:
        patch.setattr(app.state, "pool", pool)
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as probe:
            response = await probe.get("/readyz")
    assert response.status_code == 500
    assert "NOT_READY" not in response.text
    assert "synthetic defect" not in response.text


@pytest.mark.parametrize("error_type", DATASTORE_ERRORS)
async def test_shared_datastore_handler_preserves_money_path_503(
    client, monkeypatch, error_type
):
    user = await register(client)
    pool = SimpleNamespace(
        fetchval=AsyncMock(side_effect=error_type("synthetic outage"))
    )
    with monkeypatch.context() as patch:
        patch.setattr(app.state, "pool", pool)
        response = await client.get("/accounts/me", headers=auth(user["token"]))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DATASTORE_UNAVAILABLE"
    assert response.headers["Retry-After"] == "2"


@pytest.mark.parametrize(
    "kind", ["critical-header", "signature-junk", "deep-json", "non-object"]
)
async def test_malformed_signed_tokens_have_generic_401_and_no_wallet(client, kind):
    user = await register(client)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = payload_for(user["user_id"])
    if kind == "critical-header":
        header.update(crit=["unsupported-extension"], **{"unsupported-extension": True})
    elif kind == "deep-json":
        payload = b'{"nested":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
    elif kind == "non-object":
        payload = b"[]"
    token = signed_token(header, payload)
    if kind == "signature-junk":
        token += "!!!!"
    response = await client.post("/accounts", headers=auth(token))
    assert response.status_code == 401
    assert response.json() == {
        "detail": {"code": "UNAUTHORIZED", "message": "Invalid or missing credentials"}
    }
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert (
        await app.state.pool.fetchval(
            "SELECT count(*) FROM wallets WHERE user_id = $1",
            uuid.UUID(user["user_id"]),
        )
        == 0
    )


async def test_valid_trailing_signature_padding_still_authenticates(client):
    user = await register(client)
    token = signed_token({"alg": "HS256", "typ": "JWT"}, payload_for(user["user_id"]))
    header, payload, signature = token.split(".")
    signature += "=" * (-len(signature) % 4)
    response = await client.get(
        "/accounts/me", headers=auth(f"{header}.{payload}.{signature}")
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NO_WALLET"
