"""12-factor configuration: everything comes from the environment."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    app_env: str  # development | production
    token_ttl_seconds: int
    # Demo affordance: new wallets receive a starting grant so transfers are
    # demonstrable without a top-up API (kept out of scope deliberately).
    welcome_grant_paise: int
    db_pool_min: int
    db_pool_max: int


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "development")
    jwt_secret = os.environ.get("JWT_SECRET", "")
    if not jwt_secret:
        if app_env != "development":
            raise RuntimeError("JWT_SECRET must be set outside development")
        jwt_secret = "dev-only-secret-do-not-use-in-prod"
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL must be set")
    database_url = _normalize_dsn(database_url)
    return Settings(
        database_url=database_url,
        jwt_secret=jwt_secret,
        app_env=app_env,
        token_ttl_seconds=int(os.environ.get("TOKEN_TTL_SECONDS", "86400")),
        welcome_grant_paise=int(os.environ.get("WELCOME_GRANT_PAISE", "100000")),
        db_pool_min=int(os.environ.get("DB_POOL_MIN", "2")),
        db_pool_max=int(os.environ.get("DB_POOL_MAX", "10")),
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
