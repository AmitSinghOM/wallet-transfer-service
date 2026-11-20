"""Request limits at the edge: the auth limiter's arithmetic and memory bound,
and the middleware that applies it plus the body cap before any route runs.

No database: a dummy ASGI app records whether it was reached, and the limiter
runs on a hand-driven clock."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.limits import AuthLimiter, RequestLimitsMiddleware

MAX_BODY = 16


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def limiter(client=20, global_=120, max_clients=4096, clock=None):
    return AuthLimiter(client, global_, max_clients, clock=clock or FakeClock())


# ------------------------------------------------------------------ limiter


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "5", None])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_limiter_rejects_non_positive_or_non_integer_settings(bad, position):
    args = [20, 120, 4096]
    args[position] = bad
    with pytest.raises((ValueError, TypeError)):
        AuthLimiter(*args, clock=FakeClock())


def test_client_burst_then_retry_after_then_refill():
    clock = FakeClock()
    lim = limiter(client=20, clock=clock)
    assert all(lim.try_acquire("a")[0] for _ in range(20))
    assert lim.try_acquire("a") == (False, 3)
    clock.advance(2.9)
    assert lim.try_acquire("a") == (False, 1)
    clock.now = 1003.0
    assert lim.try_acquire("a")[0] is True
    assert lim.try_acquire("a")[0] is False


@pytest.mark.parametrize("rate", [7, 13, 20, 50, 120, 4096])
def test_full_burst_is_granted_whatever_the_float_interval(rate):
    lim = limiter(client=rate, global_=rate)
    assert sum(lim.try_acquire("a")[0] for _ in range(rate + 1)) == rate


def test_global_budget_spans_clients():
    lim = limiter(client=20, global_=5)
    assert all(lim.try_acquire(f"10.0.0.{i}")[0] for i in range(5))
    assert lim.try_acquire("10.0.0.99") == (False, 12)


def test_client_denial_does_not_spend_global_budget():
    lim = limiter(client=1, global_=2)
    assert lim.try_acquire("a")[0]
    assert lim.try_acquire("a") == (False, 60)
    assert lim.try_acquire("b")[0], "a's denied attempt must not consume global"
    assert lim.try_acquire("c")[0] is False


def test_global_denial_neither_spends_nor_tracks_the_client():
    clock = FakeClock()
    lim = limiter(client=1, global_=1, clock=clock)
    assert lim.try_acquire("a")[0]
    assert lim.try_acquire("b") == (False, 60)
    assert lim.tracked_clients == 1
    clock.advance(60)
    assert lim.try_acquire("b")[0], "b was never charged while globally denied"


def test_retry_after_is_the_later_of_both_limits():
    clock = FakeClock()
    lim = limiter(client=1, global_=2, clock=clock)
    assert lim.try_acquire("a")[0]
    assert lim.try_acquire("b")[0]
    clock.advance(10)
    # a: client free at +60 (50 left); global: one token back at +30 (20 left)
    assert lim.try_acquire("a") == (False, 50)


def test_full_key_table_reports_the_larger_table_or_global_wait():
    lim = limiter(client=2, global_=1, max_clients=1)
    assert lim.try_acquire("a")[0]
    assert lim.try_acquire("b") == (False, 60)


def test_full_key_table_fails_closed_without_replenishing_active_keys():
    clock = FakeClock()
    lim = limiter(client=1, global_=10_000, max_clients=2, clock=clock)
    assert lim.try_acquire("a")[0]
    clock.advance(10)
    assert lim.try_acquire("b")[0]
    assert lim.try_acquire("c") == (False, 50), "table full: fail closed"
    clock.advance(55)  # a fully replenished, b still active for 5 s
    assert lim.try_acquire("c")[0], "only a, the replenished key, is evictable"
    for i in range(1000):
        allowed, retry = lim.try_acquire(f"spray-{i}")
        assert not allowed and retry == 5
        assert lim.tracked_clients <= 2
    assert lim.try_acquire("b") == (False, 5), "eviction never refills b"


def test_clock_going_backwards_grants_nothing():
    clock = FakeClock()
    lim = limiter(client=1, clock=clock)
    assert lim.try_acquire("a")[0]
    clock.advance(-500)
    assert lim.try_acquire("a")[0] is False
    clock.advance(500 + 59)
    assert lim.try_acquire("a")[0] is False


def test_default_clock_is_monotonic():
    import inspect
    import time

    sig = inspect.signature(AuthLimiter)
    assert sig.parameters["clock"].default is time.monotonic
    assert sig.parameters["clock"].kind is inspect.Parameter.KEYWORD_ONLY


def test_concurrent_acquires_never_overspend():
    lim = limiter(client=20, global_=50, max_clients=64)
    granted = []
    start = threading.Barrier(16)

    def worker(n):
        start.wait()
        for i in range(200):
            granted.append(lim.try_acquire("shared" if i % 2 else f"k{n}-{i}")[0])

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(granted) == 50
    assert lim.tracked_clients <= 64


# --------------------------------------------------------------- middleware


class DummyApp:
    """Stands in for the route layer (validation, password hashing, DB)."""

    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        self.calls.append((scope["path"], body))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def make_app(lim=None):
    state = SimpleNamespace(
        settings=SimpleNamespace(max_body_bytes=MAX_BODY),
        auth_limiter=lim or limiter(),
    )
    return SimpleNamespace(state=state)


async def call(
    mw,
    host_app,
    path,
    chunks=(b"",),
    headers=(),
    peer=("203.0.113.7", 5000),
    disconnect_at=None,
):
    messages = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    if disconnect_at is not None:
        messages[disconnect_at:] = [{"type": "http.disconnect"}]
    reads = []

    async def receive():
        reads.append(1)
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        "client": peer,
        "app": host_app,
    }
    await mw(scope, receive, send)
    status = sent[0]["status"] if sent else None
    return SimpleNamespace(status=status, sent=sent, reads=len(reads))


def envelope(result):
    import json

    body = b"".join(m.get("body", b"") for m in result.sent[1:])
    return json.loads(body)["detail"]


def header(result, name):
    for k, v in result.sent[0]["headers"]:
        if k.decode().lower() == name:
            return v.decode()
    return None


@pytest.fixture()
def stack():
    downstream = DummyApp()
    return downstream, RequestLimitsMiddleware(downstream)


async def test_auth_paths_share_one_budget_including_trailing_slash(stack):
    downstream, mw = stack
    host = make_app(limiter(client=3))
    for path in ("/auth/login", "/auth/login/", "/auth/register/"):
        assert (await call(mw, host, path)).status == 200
    r = await call(mw, host, "/auth/register")
    assert r.status == 429
    assert envelope(r)["code"] == "RATE_LIMITED" and envelope(r)["message"]
    assert header(r, "retry-after") == "20"
    assert len(downstream.calls) == 3


async def test_rate_limit_fires_before_the_body_is_read(stack):
    downstream, mw = stack
    host = make_app(limiter(client=1))
    await call(mw, host, "/auth/login")
    r = await call(mw, host, "/auth/login", chunks=(b"x" * 999,))
    assert r.status == 429 and r.reads == 0
    assert len(downstream.calls) == 1


async def test_forwarded_for_is_ignored_and_socket_peer_is_the_key(stack):
    _, mw = stack
    host = make_app(limiter(client=1))
    assert (await call(mw, host, "/auth/login")).status == 200
    spoof = [("X-Forwarded-For", "198.51.100.9"), ("X-Real-IP", "198.51.100.9")]
    assert (await call(mw, host, "/auth/login", headers=spoof)).status == 429
    other = ("198.51.100.9", 1)
    assert (await call(mw, host, "/auth/login", peer=other)).status == 200


async def test_health_and_money_paths_are_not_auth_limited(stack):
    downstream, mw = stack
    lim = limiter(client=1, global_=1)
    host = make_app(lim)
    await call(mw, host, "/auth/login")
    for path in ("/healthz", "/readyz", "/transfers", "/auth", "/auth/logins"):
        for _ in range(5):
            assert (await call(mw, host, path)).status == 200
    assert lim.tracked_clients == 1
    assert len(downstream.calls) == 1 + 25


async def test_declared_oversize_is_refused_unread(stack):
    downstream, mw = stack
    for value in (str(MAX_BODY + 1), "9" * 40):
        r = await call(
            mw, make_app(), "/transfers", headers=[("Content-Length", value)]
        )
        assert r.status == 413 and r.reads == 0
        assert envelope(r)["code"] == "BODY_TOO_LARGE"
    assert downstream.calls == []


async def test_chunked_oversize_stops_reading_and_never_reaches_app(stack):
    downstream, mw = stack
    chunks = (b"x" * 10, b"x" * 10, b"x" * 10, b"x" * 10)
    r = await call(mw, make_app(), "/auth/register", chunks=chunks)
    assert r.status == 413 and r.reads == 2
    assert downstream.calls == []


async def test_body_at_the_cap_is_replayed_whole(stack):
    downstream, mw = stack
    chunks = (b"0123", b"", b"456789abcdef")
    headers = [("Content-Length", str(MAX_BODY))]
    assert (await call(mw, make_app(), "/t", chunks, headers)).status == 200
    assert downstream.calls == [("/t", b"0123456789abcdef")]
    r = await call(mw, make_app(), "/t", (b"x" * (MAX_BODY + 1),))
    assert r.status == 413


@pytest.mark.parametrize(
    "declared, chunks",
    [
        ("5", (b"x" * 12,)),  # lies short, under the cap
        ("5", (b"x" * 10, b"x" * 10)),  # lies short, actual beyond the cap
        ("10", (b"abc",)),  # promises more than it sends
        ("0", (b"x",)),
    ],
)
async def test_content_length_must_match_actual_bytes(stack, declared, chunks):
    downstream, mw = stack
    headers = [("Content-Length", declared)]
    r = await call(mw, make_app(), "/auth/login", chunks, headers)
    assert r.status == 400
    assert envelope(r)["code"] == "INVALID_REQUEST"
    assert downstream.calls == []


@pytest.mark.parametrize(
    "headers",
    [
        [("Content-Length", v)]
        for v in ("abc", "-1", "+5", "1.0", "", "0x10", "1 2", "\u0661")
    ]
    + [
        [("Content-Length", "3"), ("Content-Length", "4")],
        [("Content-Length", "3"), ("Transfer-Encoding", "chunked")],
    ],
)
async def test_malformed_framing_is_rejected_unread(stack, headers):
    downstream, mw = stack
    r = await call(mw, make_app(), "/auth/login", (b"abc",), headers)
    assert r.status == 400 and r.reads == 0
    assert envelope(r)["code"] == "INVALID_REQUEST"
    assert downstream.calls == []


async def test_disconnect_while_buffering_runs_nothing(stack):
    downstream, mw = stack
    r = await call(mw, make_app(), "/auth/login", (b"ab", b"cd"), disconnect_at=1)
    assert r.sent == [] and downstream.calls == []


async def test_downstream_sees_disconnect_after_replayed_body():
    seen = []

    async def app(scope, receive, send):
        seen.append(await receive())
        seen.append(await receive())

    mw = RequestLimitsMiddleware(app)
    await call(mw, make_app(), "/x", (b"hi",))
    assert seen[0] == {"type": "http.request", "body": b"hi", "more_body": False}
    assert seen[1] == {"type": "http.disconnect"}


async def test_non_http_scopes_pass_through_untouched():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    mw = RequestLimitsMiddleware(app)
    for kind in ("lifespan", "websocket"):
        await mw({"type": kind, "path": "/auth/login"}, None, None)
    assert seen == ["lifespan", "websocket"]


async def test_concurrent_auth_burst_admits_exactly_the_budget(stack):
    downstream, mw = stack
    host = make_app(limiter(client=20))
    results = await asyncio.gather(
        *(call(mw, host, "/auth/login", (b"{}",)) for _ in range(60))
    )
    statuses = [r.status for r in results]
    assert statuses.count(200) == 20 and statuses.count(429) == 40
    assert len(downstream.calls) == 20
