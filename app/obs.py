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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

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


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Assigns a correlation id per request (honouring X-Request-ID),
    emits one JSON log line per request, and records metrics."""

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request_id_var.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = time.perf_counter() - start
            route = _route_pattern(request)
            HTTP_REQUESTS.labels(request.method, route, "500").inc()
            HTTP_LATENCY.labels(request.method, route).observe(elapsed)
            log.exception(
                "request_failed",
                extra={"ctx": {"method": request.method, "path": request.url.path}},
            )
            raise
        elapsed = time.perf_counter() - start
        route = _route_pattern(request)
        HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, route).observe(elapsed)
        response.headers["X-Request-ID"] = rid
        log_event(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(elapsed * 1000, 2),
        )
        return response


def _route_pattern(request: Request) -> str:
    """Low-cardinality route label (path template, not raw path)."""
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)
