# Wallet / P2P Transfer Service

A small wallet service where users hold a balance (integer paise) and
transfer to each other — built to never lose or create money, never
double-apply a retried transfer, and never let one user move another
user's money. FastAPI + PostgreSQL.

**Design reasoning: see [WRITEUP.md](WRITEUP.md).**

## Try the live service in 60 seconds

No credentials needed — register your own users and get your own tokens.
Free-tier host: the first request after 15 idle minutes takes up to a
minute to wake; `scripts/burst.py` waits for `/readyz` before firing.

```bash
BASE=https://wallet-transfer-service-6v6c.onrender.com
J='content-type: application/json'

# 1. two users, two tokens (identity comes only from the token)
A=$(curl -s -X POST $BASE/auth/register -H "$J" -d '{"username":"grader_a_'$RANDOM'","password":"grader-pass-123"}')
B=$(curl -s -X POST $BASE/auth/register -H "$J" -d '{"username":"grader_b_'$RANDOM'","password":"grader-pass-123"}')
AT=$(echo $A | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
BT=$(echo $B | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
BID=$(echo $B | python3 -c 'import sys,json;print(json.load(sys.stdin)["user_id"])')

# 2. A's wallet (seeded with 100000 paise); B deliberately has none yet
curl -s -X POST $BASE/accounts -H "Authorization: Bearer $AT"

# 3. A -> B 25000 paise: B's wallet is created inside the transfer
curl -s -X POST $BASE/transfers -H "Authorization: Bearer $AT" -H "$J" \
  -d "{\"to_user\":\"$BID\",\"amount_paise\":25000,\"idempotency_key\":\"k1\"}"

# 4. retry same key -> same transfer_id, X-Idempotent-Replay: true, no double move
curl -s -i -X POST $BASE/transfers -H "Authorization: Bearer $AT" -H "$J" \
  -d "{\"to_user\":\"$BID\",\"amount_paise\":25000,\"idempotency_key\":\"k1\"}" | grep -iE "^HTTP|replay|transfer_id"

# 5. same key, different body -> 409
curl -s -X POST $BASE/transfers -H "Authorization: Bearer $AT" -H "$J" \
  -d "{\"to_user\":\"$BID\",\"amount_paise\":1,\"idempotency_key\":\"k1\"}"

# 6. balances reconcile: 75000 + 125000 = two seeds
curl -s $BASE/accounts/me -H "Authorization: Bearer $AT"
curl -s $BASE/accounts/me -H "Authorization: Bearer $BT"

# 7. the concurrency gate (needs: pip install httpx)
python3 scripts/burst.py $BASE
```

Also: interactive docs at `$BASE/docs`, Prometheus metrics at
`$BASE/metrics`, health at `$BASE/healthz` and `$BASE/readyz`.

## Run it locally (one command)

```bash
docker compose up --build     # app on http://localhost:8000, db included
```

Interactive API docs at `http://localhost:8000/docs`.

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/register` | `{username, password}` → `201 {user_id, token}` |
| POST | `/auth/login` | → `{user_id, token}` |
| POST | `/accounts` | get-or-create caller's wallet (idempotent) → `{balance, balance_paise}` |
| GET | `/accounts/me` | caller's balance → `{balance, balance_paise}` (same integer-paise value, two names) |
| POST | `/transfers` | `{to_user, amount_paise, idempotency_key}` → `{transfer_id, new_balance}` |
| GET | `/transfers/{id}` | participants only |
| GET | `/healthz` `/readyz` | liveness / readiness (readiness checks the DB) |
| GET | `/metrics` | Prometheus: request count, latency histogram (p99), errors, transfers applied/rejected |

All identity comes from the verified JWT (`Authorization: Bearer …`) —
never a header or body field. Status codes: `402` insufficient funds,
`409` same idempotency key with a different body, `404` unknown
recipient / not a participant, `422` validation (zero/negative amount,
self-transfer).

New wallets receive a demo starting grant (`WELCOME_GRANT_PAISE`,
default ₹1000) so transfers are demonstrable without a top-up API.

## The correctness gate (burst script)

Against any live deployment:

```bash
python scripts/burst.py https://your-deployed-url
```

Registers two brand-new users, fires 20 concurrent first-transfers
(get-or-create race) plus 20 concurrent retries of one idempotency key,
then reconciles balances. Exits 0 on PASS.

## Tests

```bash
make venv          # local virtualenv + dev deps
make test          # 15 tests incl. the concurrency gate, against a real
                   # throwaway Postgres (TEST_ADMIN_DATABASE_URL to point
                   # elsewhere; defaults to local Postgres)
```

The concurrency tests prove: wallets created exactly once under racing
first-transfers, retry storms apply once, opposite-direction transfers
don't deadlock, overspend is impossible (30 concurrent drains of a 100k
wallet → exactly 10 succeed, balance exactly 0).

## Configuration (12-factor)

All config via environment — see [.env.example](.env.example).
`JWT_SECRET` is required outside development; nothing sensitive is
committed.

## Deploy (free tier)

1. Create a free Postgres (Neon / Supabase / Railway) → copy `DATABASE_URL`.
2. Create a web service on Render / Railway / Fly.io / Koyeb pointing at
   this repo — it builds the `Dockerfile` (container image deploy, not a
   buildpack). Set env: `DATABASE_URL`, `JWT_SECRET` (long random),
   `APP_ENV=production`.
3. Migrations run automatically on container start (advisory-locked, so
   concurrent replicas are safe).
4. Logs: the platform's public log stream shows structured JSON with a
   correlation id per request.

## License

MIT — see [LICENSE](LICENSE).
