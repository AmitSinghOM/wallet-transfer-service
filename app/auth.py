"""Authentication: HS256 JWTs.

The caller's identity comes ONLY from the verified token (sub claim).
No user id is ever accepted from a header or request body for authz.
"""

import time
import uuid

import jwt
from fastapi import Depends, HTTPException, Request

from .obs import AUTH_FAILURES, log_event


def issue_token(user_id: uuid.UUID, secret: str, ttl_seconds: int) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + ttl_seconds},
        secret,
        algorithm="HS256",
    )


REQUIRED_CLAIMS = ["sub", "exp", "iat"]
NUMERIC_DATE_CLAIMS = ("exp", "iat", "nbf")


def _is_numeric_date(value: object) -> bool:
    # We only issue integer seconds. bool is an int subclass, hence `type is`.
    return type(value) is int


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
    # PyJWT int()-coerces time claims, so it accepts "123" and 1.5 and raises
    # TypeError/OverflowError on null, lists and infinity. Check the types on
    # the signature-verified payload first, then let PyJWT judge the times.
    try:
        unchecked = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={
                "require": REQUIRED_CLAIMS,
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
    except jwt.InvalidTokenError:
        raise _unauthorized("token_invalid")
    if not all(
        _is_numeric_date(unchecked[name])
        for name in NUMERIC_DATE_CLAIMS
        if name in unchecked
    ):
        raise _unauthorized("bad_numeric_date")
    try:
        claims = jwt.decode(
            token, secret, algorithms=["HS256"], options={"require": REQUIRED_CLAIMS}
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("token_expired")
    except jwt.InvalidTokenError:
        raise _unauthorized("token_invalid")
    try:
        return uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise _unauthorized("bad_subject")


CurrentUser = Depends(current_user_id)
