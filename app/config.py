"""12-factor configuration: everything comes from the environment."""

import math
import os
from dataclasses import dataclass, field

DEVELOPMENT_SECRET = "dev-only-secret-do-not-use-in-prod"


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    jwt_secret: str = field(repr=False)
    app_env: str  # development | production
    token_ttl_seconds: int
    # Demo affordance: new wallets receive a starting grant so transfers are
    # demonstrable without a top-up API (kept out of scope deliberately).
    welcome_grant_paise: int
    db_pool_min: int
    db_pool_max: int
    max_body_bytes: int
    auth_client_per_minute: int
    auth_global_per_minute: int
    auth_max_clients: int
    db_connect_timeout_seconds: float
    db_acquire_timeout_seconds: float
    db_command_timeout_seconds: float
    db_lock_timeout_seconds: float
    db_operation_timeout_seconds: float
    readiness_timeout_seconds: float


def _integer(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise RuntimeError(f"{name} must be an integer >= {minimum}") from None
    if value < minimum:
        raise RuntimeError(f"{name} must be an integer >= {minimum}")
    return value


def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        raise RuntimeError(f"{name} must be between 0.001 and 300 seconds") from None
    if not math.isfinite(value) or not 0.001 <= value <= 300:
        raise RuntimeError(f"{name} must be between 0.001 and 300 seconds")
    return value


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "production")
    if app_env not in {"development", "production"}:
        raise RuntimeError("APP_ENV must be development or production")
    jwt_secret = os.environ.get("JWT_SECRET", "")
    if not jwt_secret and app_env == "development":
        jwt_secret = DEVELOPMENT_SECRET
    if not jwt_secret.strip() or len(jwt_secret.encode("utf-8")) < 32:
        raise RuntimeError("JWT_SECRET must contain at least 32 UTF-8 bytes")
    if app_env == "production" and jwt_secret.strip() == DEVELOPMENT_SECRET:
        raise RuntimeError("JWT_SECRET must not be the public development key")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL must be set")
    minimum = _integer("DB_POOL_MIN", 2)
    maximum = _integer("DB_POOL_MAX", 10)
    if minimum > maximum:
        raise RuntimeError("DB_POOL_MIN must not exceed DB_POOL_MAX")
    return Settings(
        database_url=_normalize_dsn(database_url),
        jwt_secret=jwt_secret,
        app_env=app_env,
        token_ttl_seconds=_integer("TOKEN_TTL_SECONDS", 86400),
        welcome_grant_paise=_integer("WELCOME_GRANT_PAISE", 100000, 0),
        db_pool_min=minimum,
        db_pool_max=maximum,
        max_body_bytes=_integer("MAX_BODY_BYTES", 16384),
        auth_client_per_minute=_integer("AUTH_CLIENT_PER_MINUTE", 20),
        auth_global_per_minute=_integer("AUTH_GLOBAL_PER_MINUTE", 120),
        auth_max_clients=_integer("AUTH_MAX_CLIENTS", 4096),
        db_connect_timeout_seconds=_seconds("DB_CONNECT_TIMEOUT_SECONDS", 5),
        db_acquire_timeout_seconds=_seconds("DB_ACQUIRE_TIMEOUT_SECONDS", 1),
        db_command_timeout_seconds=_seconds("DB_COMMAND_TIMEOUT_SECONDS", 2),
        db_lock_timeout_seconds=_seconds("DB_LOCK_TIMEOUT_SECONDS", 1),
        db_operation_timeout_seconds=_seconds("DB_OPERATION_TIMEOUT_SECONDS", 5),
        readiness_timeout_seconds=_seconds("READINESS_TIMEOUT_SECONDS", 1),
    )


def _normalize_dsn(dsn: str) -> str:
    """Strip libpq-only query parameters that asyncpg would forward to the
    server as (invalid) settings. Neon's default connection string carries
    `channel_binding=require`; TLS is still enforced via sslmode."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(dsn)
    if not parts.query:
        return dsn
    kept = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit(parts._replace(query=urlencode(kept)))
