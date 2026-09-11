# Write-up — Wallet / P2P Transfer Service

## Data model

Three tables (migrations/001_init.sql). `users` (id, username, bcrypt
hash). `wallets` (user_id PK, balance_paise BIGINT ≥ 0). `transfers`
(id, from_user, to_user, amount_paise > 0, idempotency_key, body_hash,
status, from_balance_after) with `UNIQUE (from_user, idempotency_key)`.
Money is integer paise everywhere; a DB `CHECK (balance_paise >= 0)` is
the last-resort overspend guard beneath the application logic.

## Get-or-create + transfer under concurrency

Everything happens in **one Postgres transaction**: (1) get-or-create
both wallets with `INSERT … ON CONFLICT (user_id) DO NOTHING` — the
primary key arbitrates the race, so two concurrent first-transfers
create each wallet exactly once and nobody 500s; creation runs in the
same ascending-id order as the row locks, so opposite-direction first
transfers between a brand-new pair can't deadlock on the inserts;
(2) claim the
idempotency key by inserting the transfer row itself; (3) lock both
wallet rows `SELECT … FOR UPDATE` in ascending-id order (deterministic
order ⇒ no A→B/B→A deadlock), check funds, apply both balance updates.
Commit is atomic: funds move iff the idempotency record exists.

**Why this is the simplest correct mechanism:** the database's own
constraints do the arbitration; there is no coordination code to get
wrong. **Rejected heavier alternatives:** Redis/distributed locks (a
second stateful system and a new failure mode — lock expiry during a
slow commit breaks correctness); `SERIALIZABLE` isolation (correct but
needs a retry loop on 40001 for a problem row-locking solves directly);
advisory locks (reimplements what row locks already give); an async
queue/saga (exactly-once machinery for a single-database problem).

## Idempotency keys

The transfer row **is** the idempotency record — no separate table to
drift out of sync, and the key claim + balance mutation commit
atomically. Scope is `(from_user, key)`, so callers can't collide with
each other. A retry hits the unique constraint, reads the winner's
committed row, compares `body_hash` (SHA-256 of `to_user:amount`):
match ⇒ replay the original outcome (`X-Idempotent-Replay: true`);
mismatch ⇒ `409`. Rejections (insufficient funds) are persisted and
replayed too — a key names one attempt and its outcome, success *or*
failure. Keys don't expire in this exercise; production would purge
after a retry window (e.g., 24h) via partition drops.

## Identity & authorization

`Authorization: Bearer <JWT>` (HS256, secret from env, 24h expiry,
issued at register/login). The spender is **always** `sub` from the
verified token — no user id is read from headers or bodies for authz.
`GET /transfers/{id}` folds the participant check into the SQL `WHERE`;
non-participants get the same `404` as a nonexistent id (no enumeration
oracle). Passwords are bcrypt-hashed.

## Consistency vs. availability

**Writes favor consistency (CP).** If Postgres is slow or down, the
transfer path blocks then fails (`503` from `/readyz`, errors surface);
it never guesses. For money, a false "success" (double-spend, phantom
credit) is strictly worse than a refused request a client can retry —
that's why every transfer is synchronous and single-transaction.
**Reads share the same fate** here: balances come from the same
database, so a read is either current or unavailable. At real scale I'd
add a read replica/cache for `GET /accounts/me` and accept
milliseconds of staleness on display — but never make the *spend*
decision from a cache. NFR priority: correctness > availability >
latency (p99 here is single-digit ms locally; one DB round-trip
dominates).

## Edge cases handled

Insufficient funds → `402` persisted + replayed on retry; self-transfer
→ `422`; unknown recipient → `404`; zero/negative amount → `422`
(schema-validated); replay vs. conflict → `200` replay / `409` on body
mismatch; concurrent retry where the winner aborted → `409 RETRY_RACE`
(safe to retry); missing wallet on `GET /accounts/me` → `404`.

## Containerization, deploy, observability

Multi-stage Dockerfile → slim non-root image with a stdlib
`HEALTHCHECK`; `docker compose up` brings up app + db; migrations are
forward-only SQL, applied at startup under a Postgres advisory lock
(replica-safe). 12-factor: all secrets via env, nothing committed.
Structured JSON logs, one correlation id per request (honours
`X-Request-ID`), logging the meaningful events: `transfer_applied`,
`transfer_rejected_insufficient_funds`, `idempotent_replay`,
`idempotency_conflict`, `wallet_created_in_transfer`,
`get_or_create_race_lost` (a request that hit the insert conflict after
seeing the wallet absent — the loser of the race), `auth_failure`.
`/metrics` (Prometheus): request count, latency histogram (p99-capable
buckets), error rate, transfers applied/rejected by reason.

## AI usage (directed vs. decided)

I used an AI coding agent heavily for implementation. **I decided:** the
language/stack, the single-transaction design with constraint-based
arbitration, idempotency-record-in-the-transfer-row, persisting
rejections, deterministic lock ordering, CP-over-AP, the status-code
map, and the demo-grant approach to funding. **I directed the AI** to
implement those decisions, write the tests (including the concurrency
gate), and draft docs; I reviewed every file and verified the burst
gate against a live run before submitting.

## Cost

₹0. Local dev on Docker/desktop Postgres; deployment on a free web
service tier (Render/Railway/Fly/Koyeb) + free managed Postgres
(Neon/Supabase). No paid services, no credit card required.
