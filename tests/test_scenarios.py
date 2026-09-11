"""Exhaustive scenario coverage beyond the core suite: token attacks,
input validation, idempotency corner cases, further concurrency shapes,
correlation ids, and the database-level backstops."""

import asyncio
import base64
import json
import time
import uuid

import asyncpg
import jwt
import pytest

from .conftest import auth, register

pytestmark = pytest.mark.asyncio

GRANT = 100_000
DEV_SECRET = "dev-only-secret-do-not-use-in-prod"


def _tok(claims: dict, secret: str = DEV_SECRET) -> str:
    return jwt.encode(claims, secret, algorithm="HS256")


async def _pair(client):
    a, b = await register(client), await register(client)
    await client.post("/accounts", headers=auth(a["token"]))
    return a, b


def _xfer(to, amount, key):
    return {"to_user": to, "amount_paise": amount, "idempotency_key": key}


# ------------------------------------------------------------ token attacks


async def test_expired_token_401(client):
    now = int(time.time())
    t = _tok({"sub": str(uuid.uuid4()), "iat": now - 7200, "exp": now - 3600})
    assert (await client.get("/accounts/me", headers=auth(t))).status_code == 401


async def test_wrong_secret_401(client):
    now = int(time.time())
    t = _tok({"sub": str(uuid.uuid4()), "iat": now, "exp": now + 600}, "attacker")
    assert (await client.get("/accounts/me", headers=auth(t))).status_code == 401


async def test_alg_none_401(client):
    h = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    p = base64.urlsafe_b64encode(
        json.dumps({"sub": str(uuid.uuid4()), "exp": 9_999_999_999}).encode()
    )
    t = f"{h.rstrip(b'=').decode()}.{p.rstrip(b'=').decode()}."
    assert (await client.get("/accounts/me", headers=auth(t))).status_code == 401


async def test_token_without_sub_401(client):
    now = int(time.time())
    t = _tok({"iat": now, "exp": now + 600})
    assert (await client.get("/accounts/me", headers=auth(t))).status_code == 401


async def test_valid_token_for_nonexistent_user_401_not_500(client):
    """DB reset while tokens are live: signature verifies, user row gone."""
    now = int(time.time())
    ghost = _tok({"sub": str(uuid.uuid4()), "iat": now, "exp": now + 600})
    r = await client.post("/accounts", headers=auth(ghost))
    assert r.status_code == 401
    _, b = await _pair(client)
    r = await client.post(
        "/transfers", headers=auth(ghost), json=_xfer(b["user_id"], 100, "g1")
    )
    assert r.status_code == 401


async def test_basic_auth_scheme_rejected(client):
    r = await client.get("/accounts/me", headers={"Authorization": "Basic YWxpY2U6cHc="})
    assert r.status_code == 401


# ---------------------------------------------------------------- validation


async def test_register_validation(client):
    bad = [
        {"username": "ab", "password": "long-enough-9"},           # too short
        {"username": "has space", "password": "long-enough-9"},    # bad chars
        {"username": "fine_name", "password": "short"},            # short pw
        {"username": "fine_name"},                                 # missing pw
    ]
    for body in bad:
        assert (await client.post("/auth/register", json=body)).status_code == 422


async def test_transfer_validation(client):
    a, b = await _pair(client)
    h = auth(a["token"])
    cases = [
        ({"to_user": "not-a-uuid", "amount_paise": 1, "idempotency_key": "k"}, 422),
        ({"to_user": b["user_id"], "amount_paise": 100.5, "idempotency_key": "k"}, 422),
        ({"to_user": b["user_id"], "amount_paise": -1, "idempotency_key": "k"}, 422),
        ({"to_user": b["user_id"], "amount_paise": 10**13, "idempotency_key": "k"}, 422),
        ({"to_user": b["user_id"], "amount_paise": 1, "idempotency_key": ""}, 422),
        ({"to_user": b["user_id"], "amount_paise": 1, "idempotency_key": "x" * 129}, 422),
        ({"to_user": b["user_id"], "amount_paise": 1}, 422),                # no key
        ({"amount_paise": 1, "idempotency_key": "k"}, 422),                # no to_user
    ]
    for body, want in cases:
        r = await client.post("/transfers", headers=h, json=body)
        assert r.status_code == want, (body, r.status_code, r.text)
    # malformed JSON
    r = await client.post(
        "/transfers", headers={**h, "content-type": "application/json"}, content=b"{nope"
    )
    assert r.status_code == 422
    # nothing moved through any of that
    me = await client.get("/accounts/me", headers=h)
    assert me.json()["balance_paise"] == GRANT


async def test_bad_transfer_id_format_404_or_422(client):
    a, _ = await _pair(client)
    r = await client.get("/transfers/not-a-uuid", headers=auth(a["token"]))
    assert r.status_code == 422
    r = await client.get(f"/transfers/{uuid.uuid4()}", headers=auth(a["token"]))
    assert r.status_code == 404


# --------------------------------------------------------- idempotency corners


async def test_same_key_different_recipient_is_conflict(client):
    a, b = await _pair(client)
    c = await register(client)
    h = auth(a["token"])
    r1 = await client.post("/transfers", headers=h, json=_xfer(b["user_id"], 100, "k-r"))
    r2 = await client.post("/transfers", headers=h, json=_xfer(c["user_id"], 100, "k-r"))
    assert (r1.status_code, r2.status_code) == (200, 409)
    # c never got a wallet from the conflicting request
    assert (await client.get("/accounts/me", headers=auth(c["token"]))).status_code == 404


async def test_rejected_attempt_with_different_body_is_conflict(client):
    """A key that recorded a 402 is still a claimed key: changing the body
    on retry is a 409, not a fresh attempt."""
    a, b = await _pair(client)
    h = auth(a["token"])
    r1 = await client.post(
        "/transfers", headers=h, json=_xfer(b["user_id"], GRANT + 1, "k-poor")
    )
    assert r1.status_code == 402
    r2 = await client.post("/transfers", headers=h, json=_xfer(b["user_id"], 10, "k-poor"))
    assert r2.status_code == 409
    assert (await client.get("/accounts/me", headers=h)).json()["balance_paise"] == GRANT


async def test_rejected_transfer_is_readable_with_status(client):
    a, b = await _pair(client)
    h = auth(a["token"])
    r = await client.post(
        "/transfers", headers=h, json=_xfer(b["user_id"], GRANT + 1, "k-read")
    )
    tid = r.json()["detail"]["transfer_id"]
    got = await client.get(f"/transfers/{tid}", headers=h)
    assert got.status_code == 200
    assert got.json()["status"] == "rejected_insufficient_funds"
    # the recipient of a rejected transfer is still a participant
    got_b = await client.get(f"/transfers/{tid}", headers=auth(b["token"]))
    assert got_b.status_code == 200


async def test_key_reuse_after_balance_changed_still_replays_original(client):
    """Replay returns the ORIGINAL outcome, not a recomputed one."""
    a, b = await _pair(client)
    h = auth(a["token"])
    r1 = await client.post("/transfers", headers=h, json=_xfer(b["user_id"], 1_000, "k-orig"))
    await client.post("/transfers", headers=h, json=_xfer(b["user_id"], 5_000, "k-other"))
    r2 = await client.post("/transfers", headers=h, json=_xfer(b["user_id"], 1_000, "k-orig"))
    assert r1.json() == r2.json()  # new_balance frozen at the original value
    assert r2.json()["new_balance"] == GRANT - 1_000


# ----------------------------------------------------------- concurrency shapes


async def test_concurrent_post_accounts_creates_one_wallet(client, test_db_url):
    u = await register(client)
    rs = await asyncio.gather(
        *[client.post("/accounts", headers=auth(u["token"])) for _ in range(25)]
    )
    assert all(r.status_code == 200 for r in rs)
    assert len({json.dumps(r.json(), sort_keys=True) for r in rs}) == 1
    conn = await asyncpg.connect(test_db_url)
    try:
        assert await conn.fetchval(
            "SELECT count(*) FROM wallets WHERE user_id = $1", u["user_id"]
        ) == 1
    finally:
        await conn.close()


async def test_fan_out_to_many_new_recipients_conserves(client, test_db_url):
    """One sender, 15 brand-new recipients, all concurrent: every recipient
    wallet created once, total = 16 grants exactly."""
    a = await register(client)
    await client.post("/accounts", headers=auth(a["token"]))
    rcpts = [await register(client) for _ in range(15)]
    rs = await asyncio.gather(
        *[
            client.post(
                "/transfers",
                headers=auth(a["token"]),
                json=_xfer(r["user_id"], 2_000, f"fan-{i}"),
            )
            for i, r in enumerate(rcpts)
        ]
    )
    assert all(r.status_code == 200 for r in rs)
    ids = [a["user_id"]] + [r["user_id"] for r in rcpts]
    conn = await asyncpg.connect(test_db_url)
    try:
        total = await conn.fetchval(
            "SELECT sum(balance_paise) FROM wallets WHERE user_id = ANY($1::uuid[])", ids
        )
        assert total == 16 * GRANT
        n = await conn.fetchval(
            "SELECT count(*) FROM wallets WHERE user_id = ANY($1::uuid[])", ids
        )
        assert n == 16
    finally:
        await conn.close()


async def test_triangle_cycle_no_deadlock(client):
    """A->B, B->C, C->A concurrently, repeatedly: a lock cycle if ordering
    were per-transfer instead of global. Balances return to start."""
    users = [await register(client) for _ in range(3)]
    for u in users:
        await client.post("/accounts", headers=auth(u["token"]))
    tasks = []
    for i in range(8):
        for k in range(3):
            frm, to = users[k], users[(k + 1) % 3]
            tasks.append(
                client.post(
                    "/transfers",
                    headers=auth(frm["token"]),
                    json=_xfer(to["user_id"], 700, f"tri-{k}-{i}"),
                )
            )
    rs = await asyncio.gather(*tasks)
    assert all(r.status_code == 200 for r in rs), [r.status_code for r in rs]
    for u in users:
        me = await client.get("/accounts/me", headers=auth(u["token"]))
        assert me.json()["balance_paise"] == GRANT


async def test_retry_storm_on_insufficient_funds_records_once(client, test_db_url):
    """Concurrent retries of a transfer that MUST fail: one rejection row,
    every caller sees the same 402 + transfer_id, nothing moves."""
    a, b = await _pair(client)
    rs = await asyncio.gather(
        *[
            client.post(
                "/transfers",
                headers=auth(a["token"]),
                json=_xfer(b["user_id"], GRANT + 5, "storm-poor"),
            )
            for _ in range(15)
        ]
    )
    assert all(r.status_code == 402 for r in rs)
    assert len({r.json()["detail"]["transfer_id"] for r in rs}) == 1
    conn = await asyncpg.connect(test_db_url)
    try:
        assert await conn.fetchval(
            "SELECT count(*) FROM transfers WHERE idempotency_key = 'storm-poor'"
        ) == 1
    finally:
        await conn.close()
    assert (await client.get("/accounts/me", headers=auth(a["token"]))).json()[
        "balance_paise"
    ] == GRANT


async def test_mixed_storm_unique_and_retries_interleaved(client, test_db_url):
    """Unique transfers and retries of several keys all at once, both
    directions, brand-new pair. Reconcile purely from responses."""
    a, b = await register(client), await register(client)
    ha, hb = auth(a["token"]), auth(b["token"])
    tasks = []
    for i in range(6):
        for _ in range(3):  # each key retried 3x concurrently
            tasks.append(client.post("/transfers", headers=ha, json=_xfer(b["user_id"], 900, f"m-ab-{i}")))
            tasks.append(client.post("/transfers", headers=hb, json=_xfer(a["user_id"], 400, f"m-ba-{i}")))
    rs = await asyncio.gather(*tasks)
    assert all(r.status_code == 200 for r in rs), [r.status_code for r in rs]
    conn = await asyncpg.connect(test_db_url)
    try:
        n = await conn.fetchval(
            "SELECT count(*) FROM transfers WHERE idempotency_key LIKE 'm-%' "
            "AND (from_user = $1 OR from_user = $2)",
            uuid.UUID(a["user_id"]), uuid.UUID(b["user_id"]),
        )
        assert n == 12  # 6 keys each way, each applied once
    finally:
        await conn.close()
    ma = (await client.get("/accounts/me", headers=ha)).json()["balance_paise"]
    mb = (await client.get("/accounts/me", headers=hb)).json()["balance_paise"]
    assert ma == GRANT - 6 * 900 + 6 * 400
    assert mb == GRANT + 6 * 900 - 6 * 400


# ------------------------------------------------------ observability & backstop


async def test_correlation_id_is_honoured_and_generated(client):
    r = await client.get("/healthz", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers["X-Request-ID"] == "trace-abc-123"
    r = await client.get("/healthz")
    assert len(r.headers["X-Request-ID"]) == 32  # generated uuid hex


async def test_db_check_constraint_backstops_negative_balance(test_db_url):
    """Even a buggy or bypassing writer cannot push a balance negative."""
    conn = await asyncpg.connect(test_db_url)
    try:
        uid = await conn.fetchval(
            "INSERT INTO users (username, password_hash) VALUES ($1, 'x') RETURNING id",
            f"raw_{uuid.uuid4().hex[:8]}",
        )
        await conn.execute("INSERT INTO wallets (user_id, balance_paise) VALUES ($1, 10)", uid)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE wallets SET balance_paise = balance_paise - 11 WHERE user_id = $1", uid
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO transfers (from_user, to_user, amount_paise, idempotency_key, "
                "body_hash, status) VALUES ($1, $1, 0, 'z', 'h', 'pending')", uid
            )
    finally:
        await conn.close()


async def test_metrics_reflect_activity(client):
    a, b = await _pair(client)
    await client.post("/transfers", headers=auth(a["token"]), json=_xfer(b["user_id"], 1, "mtr"))
    await client.post("/transfers", headers=auth(a["token"]), json=_xfer(b["user_id"], 1, "mtr"))
    text = (await client.get("/metrics")).text
    for name in (
        "transfers_applied_total",
        "idempotent_replays_total",
        "http_request_duration_seconds_bucket",
        'http_requests_total{method="POST",route="/transfers",status="200"}',
    ):
        assert name in text, name
