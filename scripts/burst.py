#!/usr/bin/env python3
"""Correctness-gate burst: one command against a live deployment.

    python scripts/burst.py https://your-app.example.com

What it does (mirrors the graders' probe):
  1. Registers two BRAND-NEW users (no wallets yet).
  2. Fires N concurrent first-transfers A->B with unique idempotency keys —
     the get-or-create race happens inside these.
  3. Fires M concurrent retries of ONE transfer with the SAME key.
  4. Reconciles: exactly the right amount moved, retries applied once,
     wallets created exactly once, no 5xx anywhere.

Exits 0 on PASS, 1 on FAIL. Needs only httpx (pip install httpx).
"""

import asyncio
import secrets
import sys

import httpx

N_UNIQUE = 20          # concurrent first-transfers, unique keys
N_RETRIES = 20         # concurrent retries of one key
AMOUNT = 1_000         # paise per unique transfer
RETRY_AMOUNT = 7_777   # paise for the retried transfer
GRANT = 100_000        # WELCOME_GRANT_PAISE (see .env.example)


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


async def main(base: str) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=30) as c:
        # 1) two brand-new users
        users = []
        for _ in range(2):
            r = await c.post(
                "/auth/register",
                json={
                    "username": f"burst_{secrets.token_hex(6)}",
                    "password": "burst-pass-123",
                },
            )
            r.raise_for_status()
            users.append(r.json())
        a, b = users
        print(f"registered users: {a['user_id'][:8]}… -> {b['user_id'][:8]}…")
        ha = {"Authorization": f"Bearer {a['token']}"}
        hb = {"Authorization": f"Bearer {b['token']}"}

        # 2) concurrent first-transfers, unique keys (get-or-create race)
        async def unique_transfer(i: int):
            return await c.post(
                "/transfers",
                headers=ha,
                json={
                    "to_user": b["user_id"],
                    "amount_paise": AMOUNT,
                    "idempotency_key": f"burst-unique-{i}",
                },
            )

        rs = await asyncio.gather(*[unique_transfer(i) for i in range(N_UNIQUE)])
        codes = [r.status_code for r in rs]
        if any(code >= 500 for code in codes):
            fail(f"5xx during unique burst: {codes}")
        applied = sum(1 for code in codes if code == 200)
        print(f"unique burst: {applied}/{N_UNIQUE} applied, statuses={sorted(set(codes))}")

        # 3) concurrent retries of ONE key
        async def retry_transfer():
            return await c.post(
                "/transfers",
                headers=ha,
                json={
                    "to_user": b["user_id"],
                    "amount_paise": RETRY_AMOUNT,
                    "idempotency_key": "burst-same-key",
                },
            )

        rs = await asyncio.gather(*[retry_transfer() for _ in range(N_RETRIES)])
        if any(r.status_code >= 500 for r in rs):
            fail(f"5xx during retry storm: {[r.status_code for r in rs]}")
        if any(r.status_code != 200 for r in rs):
            fail(f"non-200 during retry storm: {[r.status_code for r in rs]}")
        ids = {r.json()["transfer_id"] for r in rs}
        if len(ids) != 1:
            fail(f"retry storm produced {len(ids)} distinct transfers (want 1)")
        print(f"retry storm: {N_RETRIES} retries -> 1 transfer {list(ids)[0][:8]}…")

        # 4) reconcile balances
        ra = (await c.get("/accounts/me", headers=ha)).json()
        rb = (await c.get("/accounts/me", headers=hb)).json()
        moved = applied * AMOUNT + RETRY_AMOUNT
        want_a = GRANT - moved
        want_b = GRANT + moved
        print(f"balances: A={ra['balance_paise']} (want {want_a}), "
              f"B={rb['balance_paise']} (want {want_b})")
        if ra["balance_paise"] != want_a or rb["balance_paise"] != want_b:
            fail("balances do not reconcile — money lost or created!")
        if ra["balance_paise"] + rb["balance_paise"] != 2 * GRANT:
            fail("conservation violated")
        print("PASS: conservation holds, retries applied once, no 5xx.")


if __name__ == "__main__":
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    asyncio.run(main(base_url))
