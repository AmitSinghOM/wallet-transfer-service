-- 001_init.sql — core schema for the wallet / P2P transfer service.
-- Money is INTEGER PAISE (BIGINT). Never float.

CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One wallet per user. The PRIMARY KEY on user_id is what makes
-- get-or-create race-free: two concurrent INSERT ... ON CONFLICT DO NOTHING
-- can never produce two rows, and neither ever raises.
CREATE TABLE wallets (
    user_id       UUID PRIMARY KEY REFERENCES users (id),
    balance_paise BIGINT NOT NULL DEFAULT 0 CHECK (balance_paise >= 0),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The transfer row IS the idempotency record.
-- UNIQUE (from_user, idempotency_key) arbitrates concurrent retries:
-- exactly one transaction wins the insert; losers see the unique violation,
-- read the winner's committed row, and replay its outcome.
-- 'pending' exists only inside an open transaction; committed rows are
-- always 'completed' or 'rejected_insufficient_funds'.
CREATE TABLE transfers (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_user          UUID NOT NULL REFERENCES users (id),
    to_user            UUID NOT NULL REFERENCES users (id),
    amount_paise       BIGINT NOT NULL CHECK (amount_paise > 0),
    idempotency_key    TEXT NOT NULL,
    body_hash          TEXT NOT NULL,
    status             TEXT NOT NULL CHECK
                       (status IN ('pending', 'completed', 'rejected_insufficient_funds')),
    from_balance_after BIGINT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT transfers_idempotency UNIQUE (from_user, idempotency_key),
    CONSTRAINT transfers_no_self CHECK (from_user <> to_user)
);

CREATE INDEX transfers_to_user_idx ON transfers (to_user);
