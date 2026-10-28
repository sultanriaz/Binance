-- P20-P24: all workflows are limited to research, paper, and sandbox environments.
CREATE TABLE IF NOT EXISTS governance_requests (
 id UUID PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('SANDBOX_ORDER','SHADOW_MODEL_PROMOTION')),
 requester_id BIGINT NOT NULL REFERENCES users(id), reviewer_id BIGINT REFERENCES users(id),
 payload JSONB NOT NULL, payload_sha256 CHAR(64) NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED','EXPIRED','CONSUMED')),
 review_reason TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 reviewed_at TIMESTAMPTZ, expires_at TIMESTAMPTZ NOT NULL,
 CHECK(requester_id IS DISTINCT FROM reviewer_id)
);
CREATE INDEX IF NOT EXISTS governance_pending ON governance_requests(status,created_at DESC);
CREATE TABLE IF NOT EXISTS governance_events (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 request_id UUID NOT NULL REFERENCES governance_requests(id),
 actor_id BIGINT NOT NULL REFERENCES users(id), event_type TEXT NOT NULL,
 detail JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS research_jobs (
 id UUID PRIMARY KEY, created_by BIGINT NOT NULL REFERENCES users(id),
 market TEXT NOT NULL CHECK(market IN ('spot','futures')),
 symbol TEXT NOT NULL CHECK(symbol ~ '^[A-Z0-9]{5,20}$'),
 candle_interval TEXT NOT NULL CHECK(candle_interval IN ('1m','5m','15m','1h','4h','1d')),
 bars INT NOT NULL CHECK(bars BETWEEN 350 AND 5000),
 horizon INT NOT NULL CHECK(horizon IN (1,2,3,5)),
 friction_bps DOUBLE PRECISION NOT NULL CHECK(friction_bps BETWEEN 0 AND 100),
 state TEXT NOT NULL DEFAULT 'QUEUED' CHECK(state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')),
 error_code TEXT, model_id UUID, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS research_jobs_pending ON research_jobs(state,created_at);
CREATE TABLE IF NOT EXISTS research_models (
 id UUID PRIMARY KEY, job_id UUID NOT NULL UNIQUE REFERENCES research_jobs(id),
 market TEXT NOT NULL, symbol TEXT NOT NULL, candle_interval TEXT NOT NULL,
 horizon INT NOT NULL, train_data_last_ms BIGINT NOT NULL,
 data_sha256 CHAR(64) NOT NULL, artifact JSONB NOT NULL, metrics JSONB NOT NULL,
 limitations JSONB NOT NULL, quality_pass BOOLEAN NOT NULL,
 quality_reasons JSONB NOT NULL,
 status TEXT NOT NULL DEFAULT 'CANDIDATE'
   CHECK(status IN ('CANDIDATE','SHADOW','RETIRED')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), promoted_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS research_shadow_unique ON research_models(market,symbol,candle_interval)
 WHERE status='SHADOW';
CREATE TABLE IF NOT EXISTS shadow_predictions (
 model_id UUID NOT NULL REFERENCES research_models(id),
 bar_open_ms BIGINT NOT NULL, p_up DOUBLE PRECISION NOT NULL CHECK(p_up BETWEEN 0 AND 1),
 threshold DOUBLE PRECISION NOT NULL, signal TEXT NOT NULL CHECK(signal IN ('LONG','FLAT')),
 entry_open_ms BIGINT, resolved_net_return DOUBLE PRECISION, correct_label BOOLEAN,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), resolved_at TIMESTAMPTZ,
 PRIMARY KEY(model_id,bar_open_ms)
);
CREATE INDEX IF NOT EXISTS shadow_pending ON shadow_predictions(model_id,bar_open_ms) WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS research_campaigns (
 id UUID PRIMARY KEY, created_by BIGINT NOT NULL REFERENCES users(id),
 market TEXT NOT NULL CHECK(market IN ('spot','futures')),
 symbol TEXT NOT NULL CHECK(symbol ~ '^[A-Z0-9]{5,20}$'),
 candle_interval TEXT NOT NULL CHECK(candle_interval IN ('1m','5m','15m','1h','4h','1d')),
 bars INT NOT NULL CHECK(bars BETWEEN 350 AND 5000),
 horizon INT NOT NULL CHECK(horizon IN (1,2,3,5)),
 friction_bps DOUBLE PRECISION NOT NULL CHECK(friction_bps BETWEEN 0 AND 100),
 min_new_bars INT NOT NULL CHECK(min_new_bars BETWEEN 50 AND 5000),
 enabled BOOLEAN NOT NULL DEFAULT FALSE,
 last_submission_open_ms BIGINT,
 last_job_id UUID REFERENCES research_jobs(id),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(market,symbol,candle_interval)
);
ALTER TABLE research_jobs ADD COLUMN IF NOT EXISTS campaign_id UUID REFERENCES research_campaigns(id);
INSERT INTO schema_migrations(version) VALUES ('004_intelligence') ON CONFLICT DO NOTHING;
