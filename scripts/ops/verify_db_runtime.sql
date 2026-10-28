-- Read-only operational database verification; psql -v ON_ERROR_STOP=1 -f.
DO $$
DECLARE count_migrations INTEGER;
BEGIN
 SELECT count(*) INTO count_migrations FROM schema_migrations;
 IF count_migrations < 5 THEN RAISE EXCEPTION 'Migrations incomplete'; END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='forbid_operational_evidence_mutation')
 THEN RAISE EXCEPTION 'Immutable evidence trigger absent'; END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='forbid_backup_attestation_mutation')
 THEN RAISE EXCEPTION 'Immutable backup evidence trigger absent'; END IF;
 IF EXISTS (SELECT 1 FROM exchange_order_intents
            WHERE status IN ('UNKNOWN','SUBMITTING','CANCEL_PENDING'))
 THEN RAISE EXCEPTION 'Unresolved exchange order states exist'; END IF;
 IF EXISTS (SELECT 1 FROM paper_ledger l LEFT JOIN paper_accounts a ON a.id=l.account_id
            WHERE a.id IS NULL) THEN RAISE EXCEPTION 'Orphan paper ledger'; END IF;
 IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='trader_research') THEN
   IF has_table_privilege('trader_research','exchange_order_intents','INSERT')
   THEN RAISE EXCEPTION 'Research role can submit exchange orders'; END IF;
 END IF;
END $$;
SELECT 'RUNTIME_DB_INVARIANTS_PASS' AS result;
