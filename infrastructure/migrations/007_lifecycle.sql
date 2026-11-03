-- Phase 3: Autonomous Model Lifecycle State Machine
-- Updates research_models status check constraint and adds forward shadow benchmark tracking columns.

ALTER TABLE research_models DROP CONSTRAINT IF EXISTS research_models_status_check;
ALTER TABLE research_models ADD CONSTRAINT research_models_status_check
  CHECK (status IN ('CANDIDATE', 'SHADOW', 'CANARY_ACTIVE', 'DECAYING', 'RETIRED'));

ALTER TABLE research_models ADD COLUMN IF NOT EXISTS shadow_samples_count INT NOT NULL DEFAULT 0;
ALTER TABLE research_models ADD COLUMN IF NOT EXISTS rolling_brier DOUBLE PRECISION;
ALTER TABLE research_models ADD COLUMN IF NOT EXISTS realized_win_rate DOUBLE PRECISION;

CREATE UNIQUE INDEX IF NOT EXISTS research_canary_unique ON research_models(market, symbol, candle_interval)
  WHERE status = 'CANARY_ACTIVE';

INSERT INTO schema_migrations(version) VALUES ('007_lifecycle') ON CONFLICT DO NOTHING;

