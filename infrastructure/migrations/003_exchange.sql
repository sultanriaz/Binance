-- P15–P19. TEST ENVIRONMENTS ONLY. Credentials NEVER stored in PostgreSQL.
CREATE TABLE IF NOT EXISTS exchange_accounts (
 code TEXT PRIMARY KEY CHECK (code IN ('spot-testnet','futures-demo')),
 market TEXT NOT NULL CHECK (market IN ('spot','futures')),
 environment TEXT NOT NULL CHECK (environment IN ('SPOT_TESTNET','FUTURES_DEMO')),
 operator_enabled BOOLEAN NOT NULL DEFAULT FALSE,
 state TEXT NOT NULL DEFAULT 'HALTED' CHECK (state IN ('HALTED','RECONCILING','READY','INCIDENT')),
 last_reconciled_at TIMESTAMPTZ,
 last_stream_at TIMESTAMPTZ,
 last_error TEXT,
 snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS exchange_order_intents (
 id UUID PRIMARY KEY,
 account_code TEXT NOT NULL REFERENCES exchange_accounts(code),
 client_order_id TEXT NOT NULL CHECK(length(client_order_id) BETWEEN 8 AND 36),
 payload JSONB NOT NULL,
 payload_sha256 TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'QUEUED' CHECK(status IN (
  'QUEUED','SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED','FILLED',
  'CANCEL_PENDING','CANCELED','EXPIRED','REJECTED','FAILED_POLICY')),
 exchange_order_id TEXT,
 exchange_payload JSONB,
 cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
 error_code TEXT,
 error_message TEXT,
 created_by TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(account_code, client_order_id)
);
CREATE INDEX IF NOT EXISTS exchange_intents_unresolved ON exchange_order_intents(account_code, created_at)
 WHERE status IN ('QUEUED','SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED','CANCEL_PENDING');
CREATE TABLE IF NOT EXISTS exchange_order_events (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 intent_id UUID NOT NULL REFERENCES exchange_order_intents(id),
 event_kind TEXT NOT NULL,
 payload JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS exchange_fills (
 account_code TEXT NOT NULL REFERENCES exchange_accounts(code),
 symbol TEXT NOT NULL,
 trade_id BIGINT NOT NULL,
 order_id TEXT NOT NULL,
 qty NUMERIC(36,16) NOT NULL,
 price NUMERIC(36,16) NOT NULL,
 commission NUMERIC(36,16) NOT NULL DEFAULT 0,
 commission_asset TEXT NOT NULL,
 payload JSONB NOT NULL,
 received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(account_code, symbol, trade_id)
);
CREATE TABLE IF NOT EXISTS exchange_trade_cursors (
 account_code TEXT NOT NULL REFERENCES exchange_accounts(code),
 symbol TEXT NOT NULL,
 next_id BIGINT NOT NULL CHECK(next_id >= 0),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(account_code, symbol)
);
CREATE TABLE IF NOT EXISTS exchange_incidents (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 account_code TEXT NOT NULL REFERENCES exchange_accounts(code),
 severity TEXT NOT NULL CHECK(severity IN ('WARNING','CRITICAL')),
 category TEXT NOT NULL,
 detail JSONB NOT NULL DEFAULT '{}'::jsonb,
 acknowledged_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS exchange_incidents_open ON exchange_incidents(account_code,id) WHERE acknowledged_at IS NULL;
CREATE TABLE IF NOT EXISTS exchange_stream_events (
 account_code TEXT NOT NULL REFERENCES exchange_accounts(code),
 event_key TEXT NOT NULL,
 event_type TEXT NOT NULL,
 payload JSONB NOT NULL,
 received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(account_code,event_key)
);
CREATE INDEX IF NOT EXISTS exchange_stream_events_recent ON exchange_stream_events(account_code,received_at DESC);
INSERT INTO schema_migrations(version) VALUES ('003_exchange') ON CONFLICT DO NOTHING;
