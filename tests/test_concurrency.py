"""The correctness gate, reproduced in-suite: concurrent first-transfers
between brand-new users + concurrent retries of one idempotency key.

Invariants proven here:
  - each wallet is created exactly once (never two rows, never a 500)
  - retries with one key apply exactly once
  - total money is conserved to the paise
  - overspend is impossible under concurrency
"""

import asyncio

import asyncpg
import pytest

from .conftest import auth, register

pytestmark = pytest.mark.asyncio

GRANT = 100_000


async def test_concurrent_first_transfers_between_new_pair(client, test_db_url):
    """Two brand-new users, NO pre-created wallets. Fire 20 concurrent
    transfers (unique keys) A->B: wallets must each be created exactly once
    inside the racing transfers, no 500s, balances reconcile."""
    a, b = await register(client), await register(client)

    async def fire(i: int):
        return await client.post(
            "/transfers",
            headers=auth(a["token"]),
            json={
                "to_user": b["user_id"],
                "amount_paise": 1_000,
                "idempotency_key": f"burst-{i}",
            },
        )
    responses = await asyncio.gather(*[fire(i) for i in range(20)])

    statuses = sorted(r.status_code for r in responses)
    assert all(s in (200, 402) for s in statuses), statuses  # never a 500
    applied = sum(1 for r in responses if r.status_code == 200)
    assert applied == 20  # grant covers 20 x 1000

    conn = await asyncpg.connect(test_db_url)
    try:
        for uid in (a["user_id"], b["user_id"]):
            n = await conn.fetchval(
                "SELECT count(*) FROM wallets WHERE user_id = $1", uid
            )
            assert n == 1  # exactly once, never two
        total = await conn.fetchval(
            "SELECT sum(balance_paise) FROM wallets WHERE user_id = ANY($1::uuid[])",
            [a["user_id"], b["user_id"]],
        )
        assert total == 2 * GRANT  # conservation: only the two grants exist
        bal_a = await conn.fetchval(
            "SELECT balance_paise FROM wallets WHERE user_id = $1", a["user_id"]
        )
        assert bal_a == GRANT - 20 * 1_000
    finally:
        await conn.close()


async def test_concurrent_retries_of_one_key_apply_once(client, test_db_url):
    """Many concurrent retries with the SAME idempotency key: exactly one
    application, everyone gets the same outcome, funds move once."""
    a, b = await register(client), await register(client)

    async def fire():
        return await client.post(
            "/transfers",
            headers=auth(a["token"]),
            json={
                "to_user": b["user_id"],
                "amount_paise": 7_777,
                "idempotency_key": "same-key-retry-storm",
            },
        )

    responses = await asyncio.gather(*[fire() for _ in range(20)])
    assert all(r.status_code == 200 for r in responses)
    ids = {r.json()["transfer_id"] for r in responses}
    balances = {r.json()["new_balance"] for r in responses}
    assert len(ids) == 1 and len(balances) == 1  # one outcome, replayed
    assert balances.pop() == GRANT - 7_777

    conn = await asyncpg.connect(test_db_url)
    try:
        n = await conn.fetchval(
            "SELECT count(*) FROM transfers WHERE idempotency_key = $1",
            "same-key-retry-storm",
        )
        assert n == 1
        bal_a = await conn.fetchval(
            "SELECT balance_paise FROM wallets WHERE user_id = $1", a["user_id"]
        )
        assert bal_a == GRANT - 7_777  # moved exactly once
    finally:
        await conn.close()


async def test_opposite_direction_transfers_do_not_deadlock(client):
    """A->B and B->A storms interleaved: deterministic lock ordering must
    prevent deadlocks; balances must reconcile."""
    a, b = await register(client), await register(client)
    for u in (a, b):
        await client.post("/accounts", headers=auth(u["token"]))

    async def fire(frm, to, i, tag):
        return await client.post(
            "/transfers",
            headers=auth(frm["token"]),
            json={
                "to_user": to["user_id"],
                "amount_paise": 500,
                "idempotency_key": f"x-{tag}-{i}",
            },
        )

    tasks = []
    for i in range(10):
        tasks.append(fire(a, b, i, "ab"))
        tasks.append(fire(b, a, i, "ba"))
    responses = await asyncio.gather(*tasks)
    assert all(r.status_code == 200 for r in responses), [
        r.status_code for r in responses
    ]

    ra = await client.get("/accounts/me", headers=auth(a["token"]))
    rb = await client.get("/accounts/me", headers=auth(b["token"]))
    # Symmetric volume: both end where they started; total conserved.
    assert ra.json()["balance_paise"] == GRANT
    assert rb.json()["balance_paise"] == GRANT


async def test_bidirectional_first_transfers_brand_new_pair(client, test_db_url):
    """The hardest composite case: BOTH wallets absent, concurrent transfers
    in BOTH directions. Wallet creation itself must follow the deterministic
    lock order or opposite-order inserts deadlock (40P01 -> 500). Each wallet
    created exactly once, no 5xx, conservation holds."""
    a, b = await register(client), await register(client)

    async def fire(frm, to, i, tag):
        return await client.post(
            "/transfers",
            headers=auth(frm["token"]),
            json={
                "to_user": to["user_id"],
                "amount_paise": 300,
                "idempotency_key": f"fresh-{tag}-{i}",
            },
        )

    tasks = []
    for i in range(10):
        tasks.append(fire(a, b, i, "ab"))
        tasks.append(fire(b, a, i, "ba"))
    responses = await asyncio.gather(*tasks)
    assert all(r.status_code == 200 for r in responses), [
        r.status_code for r in responses
    ]

    conn = await asyncpg.connect(test_db_url)
    try:
        for uid in (a["user_id"], b["user_id"]):
            n = await conn.fetchval(
                "SELECT count(*) FROM wallets WHERE user_id = $1", uid
            )
            assert n == 1
        total = await conn.fetchval(
            "SELECT sum(balance_paise) FROM wallets WHERE user_id = ANY($1::uuid[])",
            [a["user_id"], b["user_id"]],
        )
        assert total == 2 * GRANT
    finally:
        await conn.close()


async def test_overspend_impossible_under_concurrency(client, test_db_url):
    """Fire 30 concurrent 10k transfers from a wallet holding 100k: exactly
    10 may succeed, 20 reject with 402, balance lands on exactly 0."""
    a, b = await register(client), await register(client)
    await client.post("/accounts", headers=auth(a["token"]))

    async def fire(i):
        return await client.post(
            "/transfers",
            headers=auth(a["token"]),
            json={
                "to_user": b["user_id"],
                "amount_paise": 10_000,
                "idempotency_key": f"drain-{i}",
            },
        )

    responses = await asyncio.gather(*[fire(i) for i in range(30)])
    ok = sum(1 for r in responses if r.status_code == 200)
    rejected = sum(1 for r in responses if r.status_code == 402)
    assert (ok, rejected) == (10, 20)

    r = await client.get("/accounts/me", headers=auth(a["token"]))
    assert r.json()["balance_paise"] == 0  # never negative, never leftover
