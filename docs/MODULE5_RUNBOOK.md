# Module 5 staging and operator runbook

## New local install

Prerequisites: Docker Desktop/Engine with Compose v2.24+, Python 3.11+, Git, sufficient disk for market data. Copy `.env.example` to `.env`, set `JWT_SECRET` (random 48+ characters) and `POSTGRES_PASSWORD` (random 32+ characters). Never use production Binance keys in this repository. Then:

```bash
docker compose config --quiet
docker compose up --build -d
docker compose ps
docker compose exec -it gateway python -m trader.bootstrap_admin
```

Register a **second distinct administrator** for dual control. Browse `http://localhost:8000`; use paper mode first. MFA codes are one-time within a 90-second replay window; after logging in, wait for a new authenticator code before approving an action.

## Upgrade from Module 4

1. Preserve the existing Compose project name `aegistrade-module2`. Do not use `down -v`.
2. With an existing Module 4 deployment, **perform an out-of-band database backup before replacing code**. Since the new encrypted drill expects migration 005, it cannot replace the pre-upgrade safety backup.
3. Extract the new ZIP over the project directory, keeping your existing `.env` files outside version control. Review the new Compose services before startup.
4. `docker compose up -d --build` applies migration 005 via the idempotent migration service. Inspect its output and `/readyz`.
5. Check `docker compose logs operations-worker`, run the offline tests and verify `GET /v1/operations/readiness` with the operator session.
6. Existing paper/market/research/exchange data are retained in their original PostgreSQL volume.

## Hardened staging (not live trading)

A public DNS name must point to the staging server; allow inbound 80/443 for ACME TLS, restrict SSH to operators, and block PostgreSQL/Redis/NATS from public interfaces. Copy `.env.staging.example` to `.env.staging`. Generate unique alphanumeric/hex passwords (`python -c 'import secrets;print(secrets.token_hex(32))'`); use 48+ random characters for JWT_SECRET. Do not use `REPLACE_` placeholders.

```bash
docker compose -f docker-compose.yml -f infrastructure/staging/docker-compose.staging.yml --env-file .env.staging config --quiet
docker compose -f docker-compose.yml -f infrastructure/staging/docker-compose.staging.yml --env-file .env.staging up --build -d
```

Each service gets a distinct PostgreSQL role; the research role has no exchange order submission access. To create the initial admin interactively, use the gated one-off bootstrap profile:

```bash
docker compose -f docker-compose.yml -f infrastructure/staging/docker-compose.staging.yml --env-file .env.staging --profile bootstrap run --rm admin-bootstrap
```

Create the second admin similarly. Staging `ENABLE_TESTNET_EXECUTION=false`. For the external ingress, Caddy only serves `STAGING_HOST` with ACME certificates. Until certificate issuance and credential access are verified, do not consider staging accepted.

## Backups and restoration drill

Install the `age` CLI in the shell/WSL host running Docker; create a long-term encryption identity outside the repository, back it up offline, and store the `age1...` public recipient. Export `BACKUP_AGE_RECIPIENT` and `BACKUP_AGE_IDENTITY_FILE`. Never store the private identity in the project ZIP or source control.

```bash
./scripts/ops/backup_restore_drill.sh
```

The tool streams pg_dump into an age-encrypted archive, verifies SHA256, restores to a disposable database, checks migrated schema, then inserts an append-only owner-only attestation. It **does not** restore over `trader`, and does not prove recovery-time objectives. Off-site encrypted backups, rotation and access control require deployment policy.

Use the same Compose project name for the drill. For full disaster recovery, provision an isolated server, restore archives there, validate all application state, and only then perform an authorized cutover.

## Operations checks

```bash
PYTHONPATH=src python scripts/ops/fault_drill.py
python -m pytest -q
# runtime database checks: from repo root
# docker compose exec -T postgres psql -U trader -d trader -v ON_ERROR_STOP=1 < scripts/ops/verify_db_runtime.sql
docker compose exec gateway python -m trader.operations.verify_chain
```

Use the `Operations` tab to inspect the most recent readiness report and incidents. Prometheus rules expose critical conditions at `http://127.0.0.1:9090`; rules alone do not send email, SMS or webhooks. Configure and verify your alert receiver before marking staging accepted. Halt sandbox accounts through the Module 3 control panel on unexplained discrepancies; these operations never imply real funds were protected.

## Major incident procedure

1. Pause operator-authorized sandbox account entries and disable automated paper strategies when data is unreliable.
2. Preserve logs, account/order snapshots, database WAL/backups and model evidence. Do not restart blindly during an unresolved order timeout.
3. Review Binance exchange state directly using the applicable sandbox account and compare to local exchange ledger; reconcile unknown orders before rearming.
4. Determine root cause, restore health, and have a human resolve the operational incident with a written investigation.
5. Re-run a complete hash-chain verification and the readiness assessment. `REVIEW_REQUIRED` still requires human sign-off and grants **no** trade permission.
