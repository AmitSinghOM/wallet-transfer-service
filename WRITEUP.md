# Write-up — Wallet / P2P Transfer Service

**Data model.** Three tables (`migrations/001_init.sql`): `users` (id,
username, bcrypt hash); `wallets` (user_id PK, `balance_paise BIGINT
CHECK ≥ 0`); `transfers` (id, from_user, to_user, `amount_paise > 0`,
idempotency_key, body_hash, status, from_balance_after) with `UNIQUE
(from_user, idempotency_key)`. Integer paise everywhere; the CHECK is the
last-resort overspend guard under the application logic. Funding: the
brief has no top-up API, so a new wallet receives a fixed, logged grant
(`WELCOME_GRANT_PAISE`) — the only money-creation event, and
conservation is measured including it.

**Get-or-create + transfer.** One Postgres transaction: (1) get-or-create
both wallets with `INSERT … ON CONFLICT (user_id) DO NOTHING` — the PK
arbitrates, so racing first-transfers create each wallet exactly once and
nobody 500s; (2) claim the idempotency key by inserting the transfer row
itself; (3) `SELECT … FOR UPDATE` both wallets in ascending-id order
(deterministic ⇒ no A→B/B→A deadlock; wallet creation follows the same
order, or opposite-direction first transfers between a brand-new pair
deadlock on the inserts), check funds, apply both updates. Commit is
atomic: funds move iff the idempotency record exists. **Simplest correct
because** the database's constraints do the arbitration — no
coordination code to get wrong. **Rejected:** Redis/distributed locks (a
second stateful system; lock expiry during a slow commit breaks
correctness); `SERIALIZABLE` (correct, but needs a 40001 retry loop for a
problem row locks solve directly); advisory locks (reimplements row
locks); a queue/saga (exactly-once machinery for a single-database
problem).

**Idempotency keys.** The transfer row *is* the record — nothing to
drift, and key claim + mutation commit together. Scope `(from_user, key)`
so callers can't collide. A retry hits the unique constraint, reads the
committed row, compares `body_hash` (SHA-256 of `to_user:amount`): match
⇒ replay the original outcome with `X-Idempotent-Replay: true`; mismatch
⇒ `409`. Rejections are persisted and replayed too — a key names one
attempt and its outcome, success or failure. No expiry in this exercise;
production would purge after a retry window (~24h) via partition drops.

**Identity/authorization.** `Authorization: Bearer <JWT>` (HS256, secret
from env, 24h). The spender is always the token's `sub`; no user id is
read from headers or bodies. `GET /transfers/{id}` puts the participant
check in the SQL `WHERE`, so non-participants get the same `404` as a
nonexistent id (no enumeration oracle).

**Consistency vs. availability.** Writes are CP: if Postgres is slow or
down, transfers block then fail (`/readyz` → 503); the service never
guesses, because a false success (double-spend, phantom credit) is
strictly worse for money than a refusal the client can retry with the
same key. Reads share the database here, so a balance is current or
unavailable; at scale I'd serve `GET /accounts/me` from a replica/cache
and accept milliseconds of display staleness — but never make the spend
decision from a cache. Priority: correctness > availability > latency
(p99 is single-digit ms locally; one DB round-trip dominates).

**Edge cases.** Insufficient funds → `402`, persisted and replayed;
self-transfer → `422`; unknown recipient → `404`; zero/negative amount →
`422`; replay `200` vs. conflict `409`; a retry that collides with a
winner which then aborts → `409 RETRY_RACE` (safe to retry); no wallet on
`GET /accounts/me` → `404`.

**Container / deploy / observability.** Multi-stage Dockerfile → slim
non-root image with a stdlib `HEALTHCHECK`; `docker compose up` brings up
app + db; forward-only SQL migrations run at start under an advisory lock
(replica-safe); 12-factor, nothing sensitive committed. JSON logs with a
correlation id per request (honours `X-Request-ID`): `transfer_applied`,
`transfer_rejected_insufficient_funds`, `idempotent_replay`,
`idempotency_conflict`, `wallet_created_in_transfer`,
`get_or_create_race_lost`, `auth_failure`. `/metrics` (Prometheus):
request count, latency histogram with p99 buckets, error rate, transfers
applied/rejected by reason.

**AI usage.** I used an AI coding agent heavily. **I decided:** stack,
the single-transaction constraint-arbitrated design, idempotency record
in the transfer row, persisting rejections, deterministic lock ordering,
CP over AP, the status-code map, the grant approach. **I directed it** to
implement, write the tests (including the concurrency gate) and docs; I
reviewed every file and ran the burst gate live before submitting.

**Cost.** ₹0: local Docker/Postgres; free web tier (Render/Railway/Fly/
Koyeb) + free managed Postgres (Neon/Supabase). No card required.
