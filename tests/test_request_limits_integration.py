from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry, Counter, Histogram

from app import obs
from app.limits import AuthLimiter
from app.main import app

from .test_request_limits import FakeClock


@pytest.mark.asyncio
async def test_edge_rejections_are_observed_without_reaching_auth(monkeypatch):
    registry = CollectorRegistry()
    requests = Counter(
        "edge_requests", "test", ["method", "route", "status"], registry=registry
    )
    latency = Histogram("edge_latency", "test", ["method", "route"], registry=registry)
    monkeypatch.setattr(obs, "HTTP_REQUESTS", requests)
    monkeypatch.setattr(obs, "HTTP_LATENCY", latency)
    monkeypatch.setattr(
        app.state, "settings", SimpleNamespace(max_body_bytes=16), raising=False
    )
    monkeypatch.setattr(
        app.state,
        "auth_limiter",
        AuthLimiter(1, 10, 10, clock=FakeClock()),
        raising=False,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        large = await client.post("/auth/login", content=b"x" * 17)
        limited = await client.post("/auth/login/", content=b"x" * 17)
        health = await client.get("/healthz")
    assert large.status_code == 413
    assert large.json()["detail"]["code"] == "BODY_TOO_LARGE"
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert limited.json()["detail"]["code"] == "RATE_LIMITED"
    assert health.status_code == 200
    assert all(
        response.headers.get("X-Request-ID") for response in (large, limited, health)
    )
    assert ("POST", "unmatched", "413") in requests._metrics
    assert ("POST", "unmatched", "429") in requests._metrics
    assert ("GET", "/healthz", "200") in requests._metrics


@pytest.mark.asyncio
async def test_register_is_rejected_before_database_startup(monkeypatch):
    monkeypatch.setattr(
        app.state, "settings", SimpleNamespace(max_body_bytes=16), raising=False
    )
    monkeypatch.setattr(
        app.state,
        "auth_limiter",
        AuthLimiter(1, 10, 10, clock=FakeClock()),
        raising=False,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/auth/register", content=b"x" * 17)
        assert response.status_code == 413
        assert (await client.post("/auth/register", content=b"{}")).status_code == 429


@pytest.mark.asyncio
async def test_disconnect_during_body_buffering_never_reaches_router(monkeypatch):
    monkeypatch.setattr(
        app.state, "settings", SimpleNamespace(max_body_bytes=16), raising=False
    )
    monkeypatch.setattr(
        app.state,
        "auth_limiter",
        AuthLimiter(10, 10, 10, clock=FakeClock()),
        raising=False,
    )
    messages = iter(
        [
            {"type": "http.request", "body": b'{"username":', "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/auth/register",
        "raw_path": b"/auth/register",
        "query_string": b"",
        "headers": [],
        "client": ("203.0.113.7", 5000),
        "server": ("test", 80),
        "app": app,
    }
    await app(scope, receive, send)
    assert sent == []
