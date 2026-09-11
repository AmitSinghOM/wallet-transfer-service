# Write-up: Wallet / P2P Transfer Service

## Data model

Three tables, in `migrations/001_init.sql`. `users` has id, username and
a bcrypt hash. `wallets` is keyed by `user_id` with `balance_paise
BIGINT CHECK (>= 0)`. `transfers` has id, from_user, to_user,
`amount_paise` (checked > 0), idempotency_key, body_hash, status and
from_balance_after, unique on `(from_user, idempotency_key)`. All money
is integer paise. The balance CHECK is a floor under the application
code: if the arithmetic were ever wrong, Postgres would still refuse a
negative balance. The brief has no top-up API, so a new wallet receives
a fixed grant, `WELCOME_GRANT_PAISE`, on creation. That is the only
place money enters, it is logged, and the burst script's conservation
check includes it.

## Get-or-create and the transfer, under concurrency

A transfer is one Postgres transaction. Both wallets are get-or-created
with `INSERT ... ON CONFLICT (user_id) DO NOTHING`; the primary key
decides who wins when two first-transfers race, so each wallet is
created once and neither request fails. The idempotency key is then
claimed by inserting the transfer row itself. Both wallet rows are
locked with `SELECT ... FOR UPDATE` in ascending id order, the balance is
checked, and both updates are applied. Wallet creation follows the same
order as the locks; testing showed that creating them in from/to order
let two opposite first transfers between a brand-new pair deadlock on
the inserts. The commit is atomic, so funds move only if the idempotency
record exists, and vice versa.

This is the simplest correct mechanism because the database constraints
do the arbitration and there is no coordination code of mine to get
wrong. Rejected: a Redis or other distributed lock (a second stateful
system that fails if the lock expires during a slow commit);
`SERIALIZABLE` isolation (correct, but needs a 40001 retry loop for a
problem row locks solve directly); advisory locks (reimplement what row
locks already give here); a queue or saga (exactly-once machinery for a
single-database problem).

## Idempotency keys

The transfer row doubles as the idempotency record, so nothing can
drift and the key claim commits with the balance change. Scope is
`(from_user, key)`, so callers cannot collide. A retry hits the unique
constraint, reads the committed row and compares `body_hash`, a SHA-256
over `to_user:amount`. Match: the original outcome is returned with
`X-Idempotent-Replay: true`. Mismatch: 409. Insufficient-funds
rejections are persisted and replayed the same way, so a key names one
attempt and its outcome, success or failure. Keys do not expire here; in
production I would drop partitions after the retry window, about 24h.

## Identity and authorization

`Authorization: Bearer <JWT>`, HS256, secret from the environment, 24h
expiry. The spender is always the token's `sub`; no user id is read from
a header or body for authorization. On `GET /transfers/{id}` the
participant check is in the SQL `WHERE`, so a non-participant gets the
same 404 as a nonexistent id and cannot enumerate ids. A validly signed
token whose user no longer exists returns 401.

## Consistency versus availability

Writes favour consistency. If Postgres is slow or down, a transfer
blocks then fails with 503 and `Retry-After`, and `/readyz` reports 503;
the client retries with the same key. For money a false success (double
spend, phantom credit) costs more than a refusal the client can retry.
Reads use the same database in this build, so a balance is current or
unavailable. At scale I would serve `GET /accounts/me` from a replica or
cache and accept milliseconds of display staleness, while still making
the spend decision against the primary. Priority: correctness, then
availability, then latency. Local p99 is single-digit milliseconds; one
round trip dominates.

## Edge cases

Insufficient funds: 402, persisted, replayed. Self-transfer: 422.
Unknown recipient: 404. Zero or negative amount: 422 from validation.
Replay: 200; body mismatch: 409. A retry that collides with a winner
which then aborts: 409 `RETRY_RACE`, safe to retry. No wallet on
`GET /accounts/me`: 404.

## Container, deploy, observability

Multi-stage Dockerfile, slim non-root image, stdlib `HEALTHCHECK`.
`docker compose up` starts app and database. Forward-only SQL migrations
run at container start under an advisory lock, so replicas are safe.
All config comes from the environment; nothing sensitive is committed.
Logs are JSON with a correlation id per request (honouring
`X-Request-ID`): `transfer_applied`,
`transfer_rejected_insufficient_funds`, `idempotent_replay`,
`idempotency_conflict`, `wallet_created_in_transfer`,
`get_or_create_race_lost`, `auth_failure`, `datastore_unavailable`.
`/metrics` exposes request count, a latency histogram with p99 buckets,
error rate, and transfers applied or rejected by reason. Deployed on
Render (Singapore) with Neon Postgres in the same region; logs stream to
a public Better Stack dashboard.

## AI usage

I used an AI coding agent heavily. I decided the stack, the
single-transaction design with constraint-based arbitration, the
idempotency record in the transfer row, persisting rejections,
deterministic lock ordering, consistency over availability, the status
codes and the grant approach. I directed the agent to implement those
decisions, write the tests including the concurrency gate and draft the
documents, then reviewed every file and ran the burst gate against the
live deployment before submitting.

## Cost

Zero rupees: Render free web service, Neon free Postgres, Better Stack
free logs. No card was required.
