# Verification evidence — packaging environment

Executed on: 2026-10-28.

## Completed checks

- `python -m pytest -q` → **20 passed, 1 skipped** (API integration skipped because runtime `redis` dependency is unavailable in this restricted environment).
- `python -m compileall -q src tests scripts` → **PASS** (Python syntax compiles).
- `yaml.safe_load('docker-compose.yml')` → **PASS**; recognized seven expected service definitions. This is not equivalent to `docker compose config` or actually booting containers.
- `zipfile.ZipFile(...).testzip()` → **pending until package created**; updated during packaging.

## Unavailable in packaging environment

- Docker daemon / Docker Compose runtime: not available.
- Internet DNS/package installation for `psycopg`, `redis` and `nats-py`: not available.
- Binance public REST/WebSocket live connectivity: not tested.
- PostgreSQL/NATS/Redis integration and actual authentication API: cannot execute until dependencies and services are installed.
- `ruff` executable: unavailable, so configured lint will be checked by CI when GitHub repository is created.
- GitHub remote: not created (not supplied and requires user authorization).

## Next checks to run on target development machine

1. Replace `JWT_SECRET` and start the Compose stack.
2. `docker compose exec gateway python -m trader.db_verify`
3. Create admin, sign in with TOTP, run `python scripts/smoke.py`.
4. Review `/metrics`, outbox backlog, sequence gaps and stream reconnect status.
5. Introduce controlled Redis/NATS/collector outages and verify recovery.
6. Run CI lint/tests and record output in an actual acceptance report.

**Do not mark D1 VERIFIED or ACCEPTED until external runtime tests pass.**
