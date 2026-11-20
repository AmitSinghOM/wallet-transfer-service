from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry, Counter, Histogram

from app import obs


@pytest.mark.asyncio
async def test_unknown_paths_and_methods_have_bounded_metric_labels(monkeypatch):
    registry = CollectorRegistry()
    counter = Counter(
        "bounded_requests", "test", ["method", "route", "status"], registry=registry
    )
    histogram = Histogram(
        "bounded_duration", "test", ["method", "route"], registry=registry
    )
    monkeypatch.setattr(obs, "HTTP_REQUESTS", counter)
    monkeypatch.setattr(obs, "HTTP_LATENCY", histogram)
    app = FastAPI()
    app.add_middleware(obs.ObservabilityMiddleware)

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        return {"item": item_id}

    @app.get("/failure")
    async def failure():
        raise ValueError("synthetic defect")

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        for n in range(30):
            assert (await client.get(f"/missing-{n}")).status_code == 404
            assert (
                await client.request(f"CUSTOM{n}", f"/other-{n}")
            ).status_code == 404
            assert (await client.get(f"/items/{n}")).status_code == 200
        assert (await client.get("/failure")).status_code == 500
    assert set(counter._metrics) == {
        ("GET", "unmatched", "404"),
        ("OTHER", "unmatched", "404"),
        ("GET", "/items/{item_id}", "200"),
        ("GET", "/failure", "500"),
    }
    assert len(histogram._metrics) == 4


def test_missing_route_pattern_never_uses_raw_path():
    request = SimpleNamespace(
        scope={"route": SimpleNamespace()}, url=SimpleNamespace(path="/private")
    )
    assert obs._route_pattern(request) == "unmatched"
