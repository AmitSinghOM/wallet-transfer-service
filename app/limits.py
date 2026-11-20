"""Edge limits applied before any route parses, validates or hashes.

* Body cap (every HTTP request): the declared Content-Length and the bytes
  actually received must both stay within ``settings.max_body_bytes`` and must
  agree with each other. The body is buffered (bounded by the cap) and replayed
  to the app as one message, so nothing downstream runs, and no response has
  started, until the whole body has been checked.
* Auth limiter (``/auth/register`` and ``/auth/login``, with or without a
  trailing slash): a per-peer and a global budget, both charged atomically.
  The key is the ASGI socket peer; forwarding headers are never consulted, so
  proxy trust is decided once, by the server, not here.

Both budgets use GCRA, a token bucket expressed as a single "theoretical
arrival time" (TAT) per key: a key whose TAT has passed holds a full bucket
and is indistinguishable from an untracked key. That is the only state ever
evicted, so forgetting a key can never refill an active one. When every slot
holds an active key a new peer is refused (fail closed) rather than admitted
untracked.
"""

import heapq
import math
import threading
import time
from collections.abc import Callable

from starlette.responses import JSONResponse

AUTH_PATHS = frozenset({"/auth/register", "/auth/login"})
_MAX_LENGTH_DIGITS = 19  # anything longer is far beyond any sane cap
# 60 / rate is rarely exact in binary floating point (50/min -> 1.2 s), so a
# full burst can overshoot the window by a few ulps and lose its last grant.
_TOLERANCE_SECONDS = 1e-6


def _positive_int(name: str, value: object) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{name} must be strictly positive, got {value}")
    return value


class AuthLimiter:
    """Per-client and global per-minute budgets with a bounded key table."""

    def __init__(
        self,
        client_per_minute: int,
        global_per_minute: int,
        max_clients: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        client = _positive_int("client_per_minute", client_per_minute)
        global_ = _positive_int("global_per_minute", global_per_minute)
        self._max_clients = _positive_int("max_clients", max_clients)
        self._client_interval = 60.0 / client
        self._client_window = 60.0  # burst of `client` intervals
        self._global_interval = 60.0 / global_
        self._global_window = 60.0
        self._clock = clock
        self._lock = threading.Lock()
        self._now = float("-inf")
        self._global_tat = float("-inf")
        self._client_tat: dict[str, float] = {}
        # (tat, client) per grant; entries whose tat no longer matches the
        # table are stale and dropped when they surface.
        self._expiry: list[tuple[float, str]] = []

    @property
    def tracked_clients(self) -> int:
        return len(self._client_tat)

    def try_acquire(self, client: str) -> tuple[bool, int]:
        """Charge one attempt to ``client`` and the global budget, or neither.

        Returns ``(allowed, retry_after_seconds)``; retry_after is 0 when
        allowed and at least 1 when refused."""
        with self._lock:
            # A clock that steps backwards must not mint budget.
            now = self._now = max(self._now, self._clock())
            self._forget_replenished(now)

            tracked = client in self._client_tat
            if not tracked and len(self._client_tat) >= self._max_clients:
                new_global_tat = max(self._global_tat, now) + self._global_interval
                global_wait = new_global_tat - self._global_window - now
                return False, _seconds(max(self._expiry[0][0] - now, global_wait))

            client_tat = self._client_tat.get(client, now)
            new_client_tat = max(client_tat, now) + self._client_interval
            client_wait = new_client_tat - self._client_window - now
            new_global_tat = max(self._global_tat, now) + self._global_interval
            global_wait = new_global_tat - self._global_window - now

            wait = max(client_wait, global_wait)
            if wait > _TOLERANCE_SECONDS:
                return False, _seconds(wait)

            self._global_tat = new_global_tat
            self._client_tat[client] = new_client_tat
            heapq.heappush(self._expiry, (new_client_tat, client))
            return True, 0

    def _forget_replenished(self, now: float) -> None:
        expiry, table = self._expiry, self._client_tat
        while expiry:
            tat, client = expiry[0]
            current = table.get(client)
            if current == tat and tat > now + _TOLERANCE_SECONDS:
                return
            heapq.heappop(expiry)
            if current == tat:
                del table[client]


def _seconds(wait: float) -> int:
    return max(1, math.ceil(wait))


class _Reject(Exception):
    def __init__(self, status: int, code: str, message: str, headers=None):
        self.response = JSONResponse(
            {"detail": {"code": code, "message": message}},
            status_code=status,
            headers=headers,
        )


def _invalid(message: str) -> _Reject:
    return _Reject(400, "INVALID_REQUEST", message)


def _too_large(limit: int) -> _Reject:
    return _Reject(413, "BODY_TOO_LARGE", f"Request body exceeds {limit} bytes")


def _declared_length(headers: list[tuple[bytes, bytes]], limit: int) -> int | None:
    values = {v.strip(b" \t") for k, v in headers if k.lower() == b"content-length"}
    if not values:
        return None
    if any(k.lower() == b"transfer-encoding" for k, _ in headers):
        raise _invalid("Content-Length and Transfer-Encoding are mutually exclusive")
    if len(values) != 1:
        raise _invalid("Conflicting Content-Length headers")
    (value,) = values
    if not value or not value.isdigit():  # bytes.isdigit is ASCII-only
        raise _invalid("Malformed Content-Length")
    if len(value) > _MAX_LENGTH_DIGITS or int(value) > limit:
        raise _too_large(limit)
    return int(value)


class _Disconnected(Exception):
    pass


class RequestLimitsMiddleware:
    """Pure ASGI middleware; reads settings and limiter from ``app.state``."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope["app"].state
        limit = state.settings.max_body_bytes
        try:
            if scope["path"].rstrip("/") in AUTH_PATHS:
                self._charge_auth(state.auth_limiter, scope)
            declared = _declared_length(scope["headers"], limit)
            body = await self._read_body(receive, declared, limit)
        except _Reject as rejected:
            await rejected.response(scope, receive, send)
            return
        except _Disconnected:
            return

        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    def _charge_auth(limiter: AuthLimiter, scope) -> None:
        peer = scope.get("client")
        allowed, retry_after = limiter.try_acquire(peer[0] if peer else "-")
        if not allowed:
            raise _Reject(
                429,
                "RATE_LIMITED",
                "Too many authentication attempts; retry later",
                headers={"Retry-After": str(retry_after)},
            )

    @staticmethod
    async def _read_body(receive, declared: int | None, limit: int) -> bytes:
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise _Disconnected
            chunk = message.get("body", b"")
            size += len(chunk)
            if declared is not None and size > declared:
                raise _invalid("Request body is longer than Content-Length")
            if size > limit:
                raise _too_large(limit)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        if declared is not None and size != declared:
            raise _invalid("Request body is shorter than Content-Length")
        return b"".join(chunks)
