import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/auth/register", "/auth/login"])
@pytest.mark.parametrize("kind", ["short", "long", "object", "body", "malformed"])
async def test_validation_never_echoes_credential_input(
    monkeypatch, caplog, endpoint, kind
):
    secret = "SECRET-marker"
    value = secret if kind == "short" else secret * 20
    body = {"username": "synthetic_user", "password": value}
    if kind == "object":
        body["password"] = {"nested": secret}
    if kind == "body":
        body = [body]
    content = json.dumps(body)
    if kind == "malformed":
        content = '{"password":"' + secret + '"'
    settings = SimpleNamespace(max_body_bytes=16384)
    monkeypatch.setattr(app.state, "settings", settings, raising=False)

    class Admission:
        def try_acquire(self, client):
            return True, 0

    monkeypatch.setattr(app.state, "auth_limiter", Admission(), raising=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            endpoint, content=content, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 422
    assert secret not in response.text
    assert secret not in caplog.text
    for error in response.json()["detail"]:
        assert set(error) == {"type", "loc", "msg"}
