"""Authentication: bcrypt password hashing + HS256 JWTs.

The caller's identity comes ONLY from the verified token (sub claim).
No user id is ever accepted from a header or request body for authz.
"""

import time
import uuid

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request

from .obs import AUTH_FAILURES, log_event


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def issue_token(user_id: uuid.UUID, secret: str, ttl_seconds: int) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + ttl_seconds},
        secret,
        algorithm="HS256",
    )


def _unauthorized(reason: str) -> HTTPException:
    AUTH_FAILURES.inc()
    log_event("auth_failure", reason=reason)
    return HTTPException(
        status_code=401,
        detail={"code": "UNAUTHORIZED", "message": "Invalid or missing credentials"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def unknown_principal() -> HTTPException:
    """Token verified, but its subject has no user row."""
    return _unauthorized("unknown_principal")


async def current_user_id(request: Request) -> uuid.UUID:
    """FastAPI dependency: verified caller identity from the Bearer token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise _unauthorized("missing_bearer")
    token = auth[len("Bearer ") :]
    secret = request.app.state.settings.jwt_secret
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise _unauthorized("token_expired")
    except jwt.InvalidTokenError:
        raise _unauthorized("token_invalid")
    try:
        return uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise _unauthorized("bad_subject")


CurrentUser = Depends(current_user_id)
