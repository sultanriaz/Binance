-- P09-P14: virtual trading only. Safe to rerun on an existing Module 1 PostgreSQL volume.
CREATE TABLE IF NOT EXISTS paper_accounts (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  state JSONB NOT NULL,
  policy JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS paper_ledger (
  account_id UUID NOT NULL REFERENCES paper_accounts(id) ON DELETE RESTRICT,
  seq BIGINT NOT NULL,
  entry JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(account_id, seq)
);
CREATE TABLE IF NOT EXISTS historical_bars (
  market TEXT NOT NULL CHECK (market IN ('spot','futures')),
  symbol TEXT NOT NULL,
  interval TEXT NOT NULL,
  open_ms BIGINT NOT NULL,
  close_ms BIGINT NOT NULL,
  o NUMERIC(36,16) NOT NULL CHECK (o > 0),
  h NUMERIC(36,16) NOT NULL CHECK (h > 0),
  l NUMERIC(36,16) NOT NULL CHECK (l > 0),
  c NUMERIC(36,16) NOT NULL CHECK (c > 0),
  volume NUMERIC(36,16) NOT NULL CHECK (volume >= 0),
  source TEXT NOT NULL,
  imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (market,symbol,interval,open_ms),
  CHECK (l <= LEAST(o,c) AND h >= GREATEST(o,c) AND close_ms > open_ms)
);
CREATE INDEX IF NOT EXISTS historical_bars_recent ON historical_bars(market,symbol,interval,open_ms DESC);
CREATE TABLE IF NOT EXISTS historical_manifests (
  sha256 TEXT PRIMARY KEY,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  interval TEXT NOT NULL,
  bar_count INTEGER NOT NULL,
  first_open_ms BIGINT NOT NULL,
  last_open_ms BIGINT NOT NULL,
  details JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS paper_strategies (
  id UUID PRIMARY KEY,
  account_id UUID NOT NULL REFERENCES paper_accounts(id) ON DELETE RESTRICT,
  market TEXT NOT NULL CHECK (market IN ('spot','futures')),
  symbol TEXT NOT NULL,
  interval TEXT NOT NULL DEFAULT '1m' CHECK (interval = '1m'),
  fast_window INTEGER NOT NULL CHECK (fast_window BETWEEN 2 AND 100),
  slow_window INTEGER NOT NULL CHECK (slow_window BETWEEN 3 AND 300),
  allocation_fraction NUMERIC(10,8) NOT NULL CHECK (allocation_fraction > 0 AND allocation_fraction <= .2),
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  last_closed_bar_ms BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (fast_window < slow_window),
  UNIQUE(account_id,market,symbol,interval)
);
CREATE TABLE IF NOT EXISTS backtest_runs (
  id UUID PRIMARY KEY,
  account_id UUID REFERENCES paper_accounts(id) ON DELETE SET NULL,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  interval TEXT NOT NULL,
  settings JSONB NOT NULL,
  result JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version) VALUES ('002_paper') ON CONFLICT DO NOTHING;
