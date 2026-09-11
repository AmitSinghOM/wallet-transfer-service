# Write-up: Wallet / P2P Transfer Service

## Data model

Three tables, in `migrations/001_init.sql`. `users` holds id, username and
a bcrypt hash. `wallets` is keyed by `user_id` and holds `balance_paise`
as a `BIGINT` with `CHECK (balance_paise >= 0)`. `transfers` holds id,
from_user, to_user, `amount_paise` (checked > 0), idempotency_key,
body_hash, status and from_balance_after, with a unique constraint on
`(from_user, idempotency_key)`. All money is integer paise. The CHECK on
the balance is a floor under the application logic; if the code ever got
the arithmetic wrong, Postgres would still refuse a negative balance.

The brief has no top-up API, so a new wallet receives a fixed grant,
`WELCOME_GRANT_PAISE`, when it is created. That grant is the only place
money enters the system, it is logged, and the conservation check in the
burst script includes it.

## Get-or-create and the transfer, under concurrency

A transfer is one Postgres transaction. First, both wallets are
get-or-created with `INSERT ... ON CONFLICT (user_id) DO NOTHING`. The
primary key decides who wins when two first-transfers race, so each
wallet is created once and neither request fails. Second, the
idempotency key is claimed by inserting the transfer row itself. Third,
both wallet rows are locked with `SELECT ... FOR UPDATE` in ascending id
order, the balance is checked, and both updates are applied. Wallet
creation follows the same ascending order as the locks; I found while
testing that creating them in from/to order let two opposite first
transfers between a brand-new pair deadlock on the inserts. The commit is
atomic, so funds move only if the idempotency record exists, and vice
versa.

I think this is the simplest correct mechanism because the database
constraints do the arbitration and there is no coordination code of my
own to get wrong. I considered and rejected: a Redis or other
distributed lock, which adds a second stateful system and fails if the
lock expires during a slow commit; `SERIALIZABLE` isolation, which is
correct but needs a retry loop on 40001 for a problem that row locks
solve directly; advisory locks, which reimplement what row locks already
give me here; and a queue or saga, which is exactly-once machinery for a
single-database problem.

## Idempotency keys

The transfer row doubles as the idempotency record, so there is no
second table that can drift, and the key claim and the balance change
commit together. The key is scoped to `(from_user, key)`, so two callers
cannot collide. A retry hits the unique constraint, reads the committed
row, and compares `body_hash`, a SHA-256 over `to_user:amount`. If it
matches, the original outcome is returned with `X-Idempotent-Replay:
true`. If it differs, the response is 409. Rejections for insufficient
funds are persisted and replayed the same way, so a key always names one
attempt and its outcome, whether that outcome was success or failure.
Keys do not expire in this exercise. In production I would purge them
after the retry window, about 24 hours, by dropping partitions.

## Identity and authorization

Requests carry `Authorization: Bearer <JWT>`, HS256, secret from the
environment, 24-hour expiry. The spender is always the token's `sub`.
No user id is read from a header or a body for authorization. On
`GET /transfers/{id}` the participant check lives in the SQL `WHERE`
clause, so someone who is not a participant gets the same 404 as a
nonexistent id and cannot enumerate transfer ids. A validly signed token
whose user no longer exists returns 401.

## Consistency versus availability

Writes favour consistency. If Postgres is slow or down, a transfer
blocks and then fails with 503 and a `Retry-After` header, and `/readyz`
reports 503. The client retries with the same key. I chose this because
for money a false success (a double spend or a phantom credit) costs
more than a refusal the client can retry. Reads use the same database in
this build, so a balance is either current or unavailable. At real scale
I would serve `GET /accounts/me` from a replica or cache and accept
milliseconds of staleness on display, while still making the spend
decision against the primary. My priority order is correctness, then
availability, then latency. Locally p99 is single-digit milliseconds;
one database round trip dominates.

## Edge cases

Insufficient funds returns 402, is persisted, and replays on retry.
Self-transfer returns 422. Unknown recipient returns 404. Zero or
negative amounts return 422 from schema validation. Replay returns 200
and a body mismatch returns 409. A retry that collides with a winner
which then aborts returns 409 `RETRY_RACE`, which is safe to retry.
`GET /accounts/me` with no wallet returns 404.

## Container, deploy, observability

The Dockerfile is multi-stage and produces a slim image that runs as a
non-root user with a stdlib `HEALTHCHECK`. `docker compose up` starts
the app and the database together. Migrations are forward-only SQL,
applied at container start under a Postgres advisory lock so concurrent
replicas are safe. All configuration comes from the environment and
nothing sensitive is committed. Logs are JSON with a correlation id per
request (honouring `X-Request-ID`) and cover `transfer_applied`,
`transfer_rejected_insufficient_funds`, `idempotent_replay`,
`idempotency_conflict`, `wallet_created_in_transfer`,
`get_or_create_race_lost`, `auth_failure` and `datastore_unavailable`.
`/metrics` exposes Prometheus counters and a latency histogram with p99
buckets, plus request count, error rate and transfers applied or
rejected by reason. Deployed on Render (Singapore) with Neon Postgres in
the same region; logs stream to a public Better Stack dashboard.

## AI usage

I used an AI coding agent heavily. I decided the stack, the
single-transaction design with constraint-based arbitration, putting the
idempotency record in the transfer row, persisting rejections,
deterministic lock ordering, consistency over availability, the status
codes and the grant approach. I directed the agent to implement those
decisions, write the tests including the concurrency gate, and draft the
documents, then reviewed every file and ran the burst gate against the
live deployment before submitting.

## Cost

Zero rupees. Render free web service, Neon free Postgres, Better Stack
free logs. No card was required for any of them.
