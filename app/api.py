"""HTTP surface. Kept deliberately tiny.

Status-code choices (defended in WRITEUP.md):
  401 bad/missing token          404 unknown recipient / not-a-participant
  402 insufficient funds         409 same idempotency key, different body
  422 validation (zero/negative amount, self-transfer)
"""

import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse, Response

from . import core
from .auth import (
    current_user_id,
    hash_password,
    issue_token,
    unknown_principal,
    verify_password,
)
from .obs import log_event

router = APIRouter()

# ------------------------------------------------------------------ schemas


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)


class TransferRequest(BaseModel):
    to_user: uuid.UUID
    amount_paise: int = Field(gt=0, le=10_00_00_00_000)  # positive, sane upper bound
    idempotency_key: str = Field(min_length=1, max_length=128)


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, detail={"code": code, "message": message})


# --------------------------------------------------------------------- auth


@router.post("/auth/register", status_code=201)
async def register(body: Credentials, request: Request):
    pool: asyncpg.Pool = request.app.state.pool
    settings = request.app.state.settings
    try:
        user_id = await pool.fetchval(
            "INSERT INTO users (username, password_hash) VALUES ($1, $2) RETURNING id",
            body.username,
            hash_password(body.password),
        )
    except asyncpg.UniqueViolationError:
        raise _error(409, "USERNAME_TAKEN", "That username is already registered")
    log_event("user_registered", user_id=str(user_id))
    token = issue_token(user_id, settings.jwt_secret, settings.token_ttl_seconds)
    return {"user_id": str(user_id), "token": token}


@router.post("/auth/login")
async def login(body: Credentials, request: Request):
    pool: asyncpg.Pool = request.app.state.pool
    settings = request.app.state.settings
    row = await pool.fetchrow(
        "SELECT id, password_hash FROM users WHERE username = $1", body.username
    )
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise _error(401, "BAD_CREDENTIALS", "Unknown username or wrong password")
    token = issue_token(row["id"], settings.jwt_secret, settings.token_ttl_seconds)
    return {"user_id": str(row["id"]), "token": token}


# ----------------------------------------------------------------- accounts


@router.post("/accounts")
async def create_account(
    request: Request, caller: uuid.UUID = Depends(current_user_id)
):
    """Get-or-create the caller's wallet. Idempotent: calling twice returns
    the same wallet, never two (wallets PK arbitrates)."""
    pool: asyncpg.Pool = request.app.state.pool
    settings = request.app.state.settings
    try:
        async with pool.acquire() as conn:
            balance, created = await core.get_or_create_wallet(
                conn, caller, settings.welcome_grant_paise
            )
    except asyncpg.ForeignKeyViolationError:
        # Validly signed token for a user that no longer exists (e.g. the
        # database was reset while tokens were live). Not our identity.
        raise unknown_principal()
    if created:
        log_event("wallet_created", user_id=str(caller))
    # "balance" is the field the brief specifies; balance_paise is the same
    # integer-paise value under an unambiguous name. Both are provided.
    return {"user_id": str(caller), "balance": balance, "balance_paise": balance}


@router.get("/accounts/me")
async def my_account(request: Request, caller: uuid.UUID = Depends(current_user_id)):
    pool: asyncpg.Pool = request.app.state.pool
    balance = await pool.fetchval(
        "SELECT balance_paise FROM wallets WHERE user_id = $1", caller
    )
    if balance is None:
        raise _error(404, "NO_WALLET", "No wallet yet; POST /accounts to create one")
    return {"user_id": str(caller), "balance": balance, "balance_paise": balance}


# ---------------------------------------------------------------- transfers


@router.post("/transfers")
async def create_transfer(
    body: TransferRequest,
    request: Request,
    caller: uuid.UUID = Depends(current_user_id),
):
    pool: asyncpg.Pool = request.app.state.pool
    settings = request.app.state.settings
    try:
        outcome = await core.execute_transfer(
            pool,
            from_user=caller,
            to_user=body.to_user,
            amount_paise=body.amount_paise,
            idempotency_key=body.idempotency_key,
            grant_paise=settings.welcome_grant_paise,
        )
    except core.TransferError as e:
        raise _error(e.status_code, e.code, e.message)
    except asyncpg.ForeignKeyViolationError:
        raise unknown_principal()

    headers = {"X-Idempotent-Replay": "true"} if outcome.replayed else {}
    if outcome.status == core.STATUS_REJECTED:
        raise HTTPException(
            402,
            detail={
                "code": "INSUFFICIENT_FUNDS",
                "message": "Balance is lower than the transfer amount",
                "transfer_id": str(outcome.transfer_id),
            },
            headers=headers,
        )
    return JSONResponse(
        {
            "transfer_id": str(outcome.transfer_id),
            "new_balance": outcome.new_balance,
        },
        headers=headers,
    )


@router.get("/transfers/{transfer_id}")
async def get_transfer(
    transfer_id: uuid.UUID,
    request: Request,
    caller: uuid.UUID = Depends(current_user_id),
):
    pool: asyncpg.Pool = request.app.state.pool
    # Participant check is part of the WHERE clause: non-participants get the
    # same 404 as a nonexistent id (no transfer-id enumeration oracle).
    row = await pool.fetchrow(
        """SELECT id, from_user, to_user, amount_paise, status, created_at
           FROM transfers
           WHERE id = $1 AND (from_user = $2 OR to_user = $2)""",
        transfer_id,
        caller,
    )
    if row is None:
        raise _error(404, "NOT_FOUND", "No such transfer")
    return {
        "transfer_id": str(row["id"]),
        "from_user": str(row["from_user"]),
        "to_user": str(row["to_user"]),
        "amount_paise": row["amount_paise"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat(),
    }


# ------------------------------------------------------------ health/metrics


@router.get("/")
async def root():
    """Landing response for the bare URL: what this is and where to look."""
    return {
        "service": "wallet-transfer-service",
        "docs": "/docs",
        "health": {"liveness": "/healthz", "readiness": "/readyz"},
        "metrics": "/metrics",
        "source": "https://github.com/AmitSinghOM/wallet-transfer-service",
    }


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request):
    pool: asyncpg.Pool = request.app.state.pool
    try:
        await pool.fetchval("SELECT 1")
    except Exception:
        raise HTTPException(503, detail={"code": "NOT_READY", "message": "datastore unreachable"})
    return {"status": "ready"}


@router.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
