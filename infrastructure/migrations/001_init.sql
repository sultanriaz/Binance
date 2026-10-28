-- Division 0/1 database foundation. All trading tables are schema placeholders: no live orders.
CREATE TABLE IF NOT EXISTS users (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('ADMIN','VIEWER')),
  totp_secret TEXT,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS instruments (
  market TEXT NOT NULL CHECK (market IN ('spot','futures')),
  symbol TEXT NOT NULL,
  base_asset TEXT NOT NULL,
  quote_asset TEXT NOT NULL,
  status TEXT NOT NULL,
  filters JSONB NOT NULL DEFAULT '[]'::jsonb,
  raw JSONB NOT NULL,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (market, symbol)
);
CREATE TABLE IF NOT EXISTS market_events (
  event_id TEXT PRIMARY KEY,
  market TEXT NOT NULL CHECK (market IN ('spot','futures')),
  symbol TEXT NOT NULL,
  event_type TEXT NOT NULL,
  exchange_ts BIGINT,
  received_ts BIGINT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS market_events_lookup ON market_events(market,symbol,event_type,received_ts DESC);
CREATE TABLE IF NOT EXISTS market_latest (
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_id TEXT NOT NULL,
  exchange_ts BIGINT,
  received_ts BIGINT NOT NULL,
  payload JSONB NOT NULL,
  PRIMARY KEY (market, symbol, event_type)
);
CREATE TABLE IF NOT EXISTS stream_status (
  market TEXT NOT NULL PRIMARY KEY,
  connected BOOLEAN NOT NULL DEFAULT FALSE,
  last_event_ts BIGINT,
  last_connected_at TIMESTAMPTZ,
  last_error TEXT,
  reconnects BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS outbox_events (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id TEXT UNIQUE NOT NULL REFERENCES market_events(event_id),
  subject TEXT NOT NULL,
  data JSONB NOT NULL,
  published_at TIMESTAMPTZ,
  retry_count INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS outbox_unpublished ON outbox_events(id) WHERE published_at IS NULL;
CREATE TABLE IF NOT EXISTS audit_logs (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  outcome TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO schema_migrations(version) VALUES ('001_init') ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS aggregate_trade_offsets (
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  last_id BIGINT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(market,symbol)
);
CREATE TABLE IF NOT EXISTS market_data_gaps (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  category TEXT NOT NULL,
  expected_next BIGINT NOT NULL,
  observed_next BIGINT NOT NULL,
  detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS unresolved_gaps ON market_data_gaps(market,symbol) WHERE resolved_at IS NULL;
