# Module 4 — Verification record

Module 4 is **not** certified end-to-end against a live Docker/PostgreSQL/Binance environment in the source-authoring container. The acceptance checklist must be executed independently before claiming P20–P24 are VERIFIED/ACCEPTED.

Offline unit tests validate feature leakage invariants, trained model JSON integrity, deterministic replay, overlap prevention, independent approval invariants, and inherited Module 1–3 behavior. The repo bundles no test credentials or secret values.

Commands:

```bash
python -m pip install -e '.[dev,research]'
python -m pytest -q
python -m compileall -q src
node --check web/terminal.js
docker compose config --quiet
```

Runtime tests requiring PostgreSQL / Redis / NATS / Binance test credentials are intentionally **PENDING** and must not be inferred from these offline tests.
