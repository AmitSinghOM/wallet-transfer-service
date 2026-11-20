import pytest

from app.config import load_settings


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    import os

    for key in os.environ:
        if key.startswith(("DB_", "AUTH_")) or key in {
            "APP_ENV",
            "JWT_SECRET",
            "TOKEN_TTL_SECONDS",
            "WELCOME_GRANT_PAISE",
            "MAX_BODY_BYTES",
            "READINESS_TIMEOUT_SECONDS",
        }:
            monkeypatch.delenv(key)
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic.invalid/wallet")


@pytest.mark.parametrize("environment", [None, "production"])
@pytest.mark.parametrize(
    "secret", [None, "x", "x" * 31, " " * 32, "dev-only-secret-do-not-use-in-prod"]
)
def test_production_rejects_missing_public_or_short_secret(
    monkeypatch, environment, secret
):
    if environment is not None:
        monkeypatch.setenv("APP_ENV", environment)
    if secret is not None:
        monkeypatch.setenv("JWT_SECRET", secret)
    with pytest.raises(RuntimeError) as error:
        load_settings()
    if secret:
        assert secret not in str(error.value)


def test_development_fallback_requires_explicit_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    assert load_settings().jwt_secret == "dev-only-secret-do-not-use-in-prod"


@pytest.mark.parametrize("environment", ["", "Production", "staging", "prod"])
def test_unknown_environment_is_not_a_development_escape(monkeypatch, environment):
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setenv("JWT_SECRET", "valid-synthetic-test-key-32-bytes!!")
    with pytest.raises(RuntimeError):
        load_settings()


def test_secret_length_is_utf8_bytes_and_default_is_production(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "界" * 11)
    settings = load_settings()
    assert settings.app_env == "production"
    assert settings.max_body_bytes == 16384
    assert settings.auth_client_per_minute == 20
    assert settings.auth_global_per_minute == 120
    assert settings.auth_max_clients == 4096


@pytest.mark.parametrize(
    "key",
    [
        "DB_ACQUIRE_TIMEOUT_SECONDS",
        "DB_OPERATION_TIMEOUT_SECONDS",
        "DB_COMMAND_TIMEOUT_SECONDS",
        "DB_CONNECT_TIMEOUT_SECONDS",
        "DB_LOCK_TIMEOUT_SECONDS",
        "READINESS_TIMEOUT_SECONDS",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "invalid"])
def test_database_budgets_must_be_finite_positive(monkeypatch, key, value):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError):
        load_settings()


@pytest.mark.parametrize(
    "key",
    [
        "AUTH_CLIENT_PER_MINUTE",
        "AUTH_GLOBAL_PER_MINUTE",
        "AUTH_MAX_CLIENTS",
        "MAX_BODY_BYTES",
        "DB_POOL_MAX",
        "TOKEN_TTL_SECONDS",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "1.5"])
def test_resource_limits_cannot_be_disabled_by_invalid_values(monkeypatch, key, value):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError):
        load_settings()


def test_invalid_pool_order_is_rejected(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DB_POOL_MIN", "3")
    monkeypatch.setenv("DB_POOL_MAX", "2")
    with pytest.raises(RuntimeError):
        load_settings()
