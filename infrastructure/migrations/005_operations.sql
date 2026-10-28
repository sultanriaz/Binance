-- P25–P28 operational evidence only. Never authorizes exchange orders.
CREATE TABLE IF NOT EXISTS service_heartbeats (
 service TEXT PRIMARY KEY CHECK (service ~ '^[a-z][a-z0-9_-]{1,40}$'),
 last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
 status TEXT NOT NULL CHECK (status IN ('HEALTHY','DEGRADED','HALTED')),
 detail JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS operations_readiness (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 verdict TEXT NOT NULL CHECK (verdict IN ('NO_GO','REVIEW_REQUIRED')),
 policy_version TEXT NOT NULL,
 evidence JSONB NOT NULL,
 sha256 CHAR(64) NOT NULL,
 prior_sha256 CHAR(64),
 CHECK (sha256 ~ '^[0-9a-f]{64}$')
);
CREATE INDEX IF NOT EXISTS operations_readiness_latest ON operations_readiness(evaluated_at DESC);
CREATE TABLE IF NOT EXISTS backup_attestations (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 archive_sha256 CHAR(64) NOT NULL CHECK (archive_sha256 ~ '^[0-9a-f]{64}$'),
 method TEXT NOT NULL CHECK (method = 'DISPOSABLE_FULL_RESTORE'),
 verified_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS operations_incidents (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 component TEXT NOT NULL, code TEXT NOT NULL,
 details JSONB NOT NULL DEFAULT '{}'::jsonb,
 opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 resolved_at TIMESTAMPTZ,
 resolved_by BIGINT REFERENCES users(id),
 CHECK (resolved_at IS NULL OR resolved_by IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS operations_open_incident_unique
 ON operations_incidents(component,code) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS operations_open_incidents ON operations_incidents(opened_at DESC)
 WHERE resolved_at IS NULL;
-- Evidence snapshots have no updates or deletes from the application.
CREATE OR REPLACE FUNCTION refuse_operational_evidence_changes()
 RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 RAISE EXCEPTION 'readiness evidence is immutable';
 END $$;
DROP TRIGGER IF EXISTS forbid_operational_evidence_mutation ON operations_readiness;
CREATE TRIGGER forbid_operational_evidence_mutation
 BEFORE UPDATE OR DELETE ON operations_readiness
 FOR EACH ROW EXECUTE FUNCTION refuse_operational_evidence_changes();
DROP TRIGGER IF EXISTS forbid_backup_attestation_mutation ON backup_attestations;
CREATE TRIGGER forbid_backup_attestation_mutation
 BEFORE UPDATE OR DELETE ON backup_attestations
 FOR EACH ROW EXECUTE FUNCTION refuse_operational_evidence_changes();
INSERT INTO schema_migrations(version) VALUES ('005_operations') ON CONFLICT DO NOTHING;
