# Module 5 (P25–P28) — Source implementation release

Adds security, deployment, fail-closed monitoring and shadow-readiness code to the complete Module 4 repository. Preserves Module 1–4 functionality and existing Compose volume name for safe upgrades.

## New or updated

- `src/trader/operations/`: HTTP security middleware, one-use MFA, closed-bar admission, evidence policy, DB repository, worker, operator endpoints and complete-chain audit.
- `infrastructure/migrations/005_operations.sql`: heartbeats, incidents, immutable readiness evidence and owner-controlled backup attestations.
- `scripts/ops/`: staging PostgreSQL least-privilege provisioning, encrypted backup and disposable restore, offline fault drill, evidence verifier and DB invariant checker.
- `infrastructure/staging/`: Caddy TLS ingress and segregated-service staging deployment overlay.
- `infrastructure/alerts/`: Prometheus alarm definitions; operator notification channels require manual connection.
- `web/`: shadow readiness and incident dashboard.
- `.github/workflows/security.yml`: recurring dependency audit; CI updated for fault-test command.

## Safeguards and limitations

- **No production Spot/Futures order execution** added.
- P25–P28 implemented in source; Docker/staging/real DB/sandbox acceptance not yet verified.
- Research and shadow agents remain unable to submit exchange orders.
- `NO_GO` is the default; `REVIEW_REQUIRED` never authorizes trading.
- Backups require an age identity and successful actual restore to record an attestation.
- TOTP secrets are not end-to-end encrypted; production release requires separate secret-management review.

## Recorded local source checks

- `pytest -q`: **109 passed, 1 skipped** (the skipped case is inherited and depends on an unavailable optional test dependency).
- Standalone P26 fault drill: **10/10 injected gate faults correctly refused**, zero exchange requests.
- Python compileall and JavaScript syntax checks: passed.
- Bash backup script static syntax check: passed. Actual backup + encrypted restore: **not run**.
- Compose, Prometheus, Caddy and alert YAML structure: parsed locally. Real Docker Compose semantic/runtime validation: **not run**.
- Actual PostgreSQL migration/permission checks, Docker, staging TLS and Binance testnet: **not run**.
