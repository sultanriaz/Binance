#!/usr/bin/env bash
# Encrypted backup + ACTUAL disposable DB restore. Requires the age CLI and an
# OFF-REPOSITORY private identity file. Does not stop or alter the source DB.
set -euo pipefail
umask 077
[[ -f docker-compose.yml ]] || { echo 'Run from repository root' >&2; exit 2; }
command -v age >/dev/null || { echo 'Install age before attempting backups' >&2; exit 2; }
command -v sha256sum >/dev/null || exit 2
: "${BACKUP_AGE_RECIPIENT:?Set age public recipient}"
: "${BACKUP_AGE_IDENTITY_FILE:?Set private age identity path outside repo}"
[[ -r "$BACKUP_AGE_IDENTITY_FILE" ]] || { echo 'Backup decryption identity unavailable' >&2; exit 2; }
mkdir -p backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
artifact="backups/trader-${stamp}.dump.age"
verify_db="trader_verify_$(date -u +%s)"
[[ "$verify_db" =~ ^trader_verify_[0-9]+$ ]] || exit 2
cleanup() {
  docker compose exec -T postgres psql -U trader -d trader -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS ${verify_db} WITH (FORCE);" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Plaintext never touches the host disk; encrypted archive has restrictive permissions.
docker compose exec -T postgres pg_dump -U trader -d trader -Fc --no-owner --no-acl | \
  age -r "$BACKUP_AGE_RECIPIENT" -o "$artifact"
[[ -s "$artifact" ]] || { echo 'Backup empty' >&2; exit 3; }
sha256sum "$artifact" > "${artifact}.sha256"
sha256sum -c "${artifact}.sha256"
age -d -i "$BACKUP_AGE_IDENTITY_FILE" "$artifact" | docker compose exec -T postgres pg_restore --list >/dev/null
# Validate the FULL physical restore into a disposable database.
docker compose exec -T postgres psql -U trader -d trader -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${verify_db};" >/dev/null
age -d -i "$BACKUP_AGE_IDENTITY_FILE" "$artifact" | \
  docker compose exec -T postgres pg_restore -U trader -d "$verify_db" --no-owner --no-acl --exit-on-error
# Verify the actual restored schema and base accounting tables.
docker compose exec -T postgres psql -U trader -d "$verify_db" -v ON_ERROR_STOP=1 -Atc \
  "SELECT count(*)>=5 FROM schema_migrations; SELECT to_regclass('public.paper_ledger') IS NOT NULL; SELECT to_regclass('public.operations_readiness') IS NOT NULL;" | \
  awk 'BEGIN{n=0} $0=="t"{n++} END{exit (n==3)?0:1}' || \
  { echo 'Restored DB missing required schema; backup NOT accepted' >&2; exit 4; }
# Attestation only after successful full restoration; never use this as proof of actual disaster recovery time.
checksum="$(cut -d' ' -f1 "${artifact}.sha256")"
docker compose exec -T postgres psql -U trader -d trader -v ON_ERROR_STOP=1 -c \
 "INSERT INTO backup_attestations(archive_sha256,method) VALUES('${checksum}','DISPOSABLE_FULL_RESTORE');" >/dev/null
printf 'ENCRYPTED backup and disposable restore VERIFIED. Artifact: %s SHA256: %s\n' "$artifact" "$checksum"
