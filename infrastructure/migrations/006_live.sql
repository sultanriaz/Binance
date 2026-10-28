-- P29-P32. Live capability is independent from sandbox; disabled until runtime and human approvals.
CREATE TABLE IF NOT EXISTS live_accounts (
 code TEXT PRIMARY KEY CHECK(code IN ('spot-live','futures-live')),
 state TEXT NOT NULL DEFAULT 'HALTED' CHECK(state IN ('HALTED','RECONCILING','READY','INCIDENT')),
 armed BOOLEAN NOT NULL DEFAULT FALSE,
 automation_enabled BOOLEAN NOT NULL DEFAULT FALSE,
 last_reconciled_at TIMESTAMPTZ,
 last_error TEXT, snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
 equity_baseline NUMERIC(36,12), equity_baseline_day DATE,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO live_accounts(code) VALUES('spot-live'),('futures-live') ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS live_authorizations (
 id UUID PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN
 ('SPOT_ARM','FUTURES_ARM','SPOT_AUTOMATION','SPOT_ORDER','FUTURES_ORDER')),
 requester BIGINT NOT NULL REFERENCES users(id), reviewer BIGINT REFERENCES users(id),
 payload JSONB NOT NULL, payload_sha CHAR(64) NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED','CONSUMED','EXPIRED')),
 reason TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ NOT NULL, reviewed_at TIMESTAMPTZ, consumed_at TIMESTAMPTZ,
 CHECK(requester <> reviewer)
);
CREATE INDEX IF NOT EXISTS live_auth_pending ON live_authorizations(status,expires_at);
CREATE TABLE IF NOT EXISTS live_authorization_events (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 authorization_id UUID NOT NULL REFERENCES live_authorizations(id),
 actor BIGINT REFERENCES users(id), event TEXT NOT NULL, recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 detail JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS live_order_intents (
 id UUID PRIMARY KEY, account_code TEXT NOT NULL REFERENCES live_accounts(code),
 client_order_id TEXT NOT NULL CHECK(length(client_order_id) BETWEEN 8 AND 36),
 payload JSONB NOT NULL, payload_sha CHAR(64) NOT NULL,
 source TEXT NOT NULL CHECK(source IN ('HUMAN','AUTOMATION','PROTECTIVE')),
 authorization_id UUID REFERENCES live_authorizations(id),
 status TEXT NOT NULL DEFAULT 'QUEUED' CHECK(status IN (
  'QUEUED','SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED',
  'FILLED','CANCEL_PENDING','CANCELED','EXPIRED','REJECTED','FAILED_POLICY')),
 exchange_order_id TEXT, response JSONB,
 cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
 error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(account_code,client_order_id)
);
CREATE INDEX IF NOT EXISTS live_intents_open ON live_order_intents(account_code,created_at)
 WHERE status IN ('QUEUED','SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED','CANCEL_PENDING');
CREATE TABLE IF NOT EXISTS live_order_events (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 intent_id UUID NOT NULL REFERENCES live_order_intents(id),
 status TEXT NOT NULL, detail JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS live_fills (
 account_code TEXT NOT NULL REFERENCES live_accounts(code),symbol TEXT NOT NULL,
 trade_id BIGINT NOT NULL,order_id TEXT NOT NULL,quantity NUMERIC(36,16) NOT NULL,
 price NUMERIC(36,16) NOT NULL,fee NUMERIC(36,16) NOT NULL,
 asset TEXT NOT NULL,payload JSONB NOT NULL, recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(account_code,symbol,trade_id)
);
CREATE TABLE IF NOT EXISTS live_fill_cursors (
 account_code TEXT NOT NULL REFERENCES live_accounts(code),symbol TEXT NOT NULL,
 next_id BIGINT NOT NULL,PRIMARY KEY(account_code,symbol)
);
CREATE TABLE IF NOT EXISTS live_incidents (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 account_code TEXT NOT NULL REFERENCES live_accounts(code),
 category TEXT NOT NULL, detail JSONB NOT NULL DEFAULT '{}'::jsonb,
 raised_at TIMESTAMPTZ NOT NULL DEFAULT now(), acknowledged_at TIMESTAMPTZ,
 acknowledged_by BIGINT REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS live_automation (
 account_code TEXT PRIMARY KEY REFERENCES live_accounts(code) CHECK (account_code='spot-live'),
 symbol TEXT NOT NULL, candle_interval TEXT NOT NULL DEFAULT '1m',
 quantity NUMERIC(36,16) NOT NULL CHECK(quantity>0),
 fast_window INTEGER NOT NULL CHECK(fast_window BETWEEN 5 AND 20),
 slow_window INTEGER NOT NULL CHECK(slow_window BETWEEN 21 AND 100),
 last_candle BIGINT NOT NULL DEFAULT 0,
 config_sha CHAR(64) NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Approval and order histories are append-only. Audit events cannot be edited by application users.
CREATE OR REPLACE FUNCTION live_prevent_audit_mutation() RETURNS trigger AS $$
 BEGIN RAISE EXCEPTION 'Live audit events cannot be changed'; END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS live_auth_events_immutable ON live_authorization_events;
CREATE TRIGGER live_auth_events_immutable BEFORE UPDATE OR DELETE ON live_authorization_events
 FOR EACH ROW EXECUTE FUNCTION live_prevent_audit_mutation();
DROP TRIGGER IF EXISTS live_order_events_immutable ON live_order_events;
CREATE TRIGGER live_order_events_immutable BEFORE UPDATE OR DELETE ON live_order_events
 FOR EACH ROW EXECUTE FUNCTION live_prevent_audit_mutation();
INSERT INTO schema_migrations(version) VALUES ('006_live') ON CONFLICT DO NOTHING;
