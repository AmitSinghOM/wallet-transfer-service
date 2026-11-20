"""Observability: structured JSON logs with a correlation id per request,
and Prometheus metrics (request count, latency histogram incl. p99 buckets,
error rate, transfers applied/rejected).
"""

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar

from prometheus_client import Counter, Histogram
from starlette.datastructures import Headers, MutableHeaders

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# ---------------------------------------------------------------- metrics

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
TRANSFERS_APPLIED = Counter("transfers_applied_total", "Transfers applied")
TRANSFERS_REJECTED = Counter(
    "transfers_rejected_total", "Transfers rejected", ["reason"]
)
IDEMPOTENT_REPLAYS = Counter(
    "idempotent_replays_total", "Transfer requests answered from the idempotency record"
)
AUTH_FAILURES = Counter("auth_failures_total", "Authentication failures")

# ---------------------------------------------------------------- logging


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        extra = getattr(record, "ctx", None)
        if extra:
            entry.update(extra)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # uvicorn's default access log is redundant with our request log
    logging.getLogger("uvicorn.access").disabled = True


log = logging.getLogger("wallet")


def log_event(event: str, **ctx) -> None:
    """Log a meaningful business event as structured JSON."""
    log.info(event, extra={"ctx": ctx})


# ---------------------------------------------------------------- middleware


class ObservabilityMiddleware:
    """Records bounded request metrics and structured request logs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rid = Headers(scope=scope).get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        method = scope["method"] if scope["method"] in _HTTP_METHODS else "OTHER"
        start = time.perf_counter()
        status = None

        async def observed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = rid
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except Exception:
            elapsed = time.perf_counter() - start
            route = _route_pattern(scope)
            HTTP_REQUESTS.labels(method, route, "500").inc()
            HTTP_LATENCY.labels(method, route).observe(elapsed)
            log.exception(
                "request_failed",
                extra={"ctx": {"method": scope["method"], "path": scope["path"]}},
            )
            raise
        else:
            if status is not None:
                elapsed = time.perf_counter() - start
                route = _route_pattern(scope)
                HTTP_REQUESTS.labels(method, route, str(status)).inc()
                HTTP_LATENCY.labels(method, route).observe(elapsed)
                log_event(
                    "http_request",
                    method=scope["method"],
                    path=scope["path"],
                    status=status,
                    duration_ms=round(elapsed * 1000, 2),
                )
        finally:
            request_id_var.reset(token)


_HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)


def _route_pattern(request_or_scope) -> str:
    """Low-cardinality route label (path template, not raw path)."""
    scope = (
        request_or_scope
        if isinstance(request_or_scope, dict)
        else request_or_scope.scope
    )
    route = scope.get("route")
    return getattr(route, "path", None) or "unmatched"
