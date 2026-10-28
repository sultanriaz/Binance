# Module 3 verification record

The sandbox source, REST transport, deterministic preflight, order state machine, spot/futures adapters, PostgreSQL schema and UI are implemented. The automated offline suite exercises the original read-only/paper modules and the Module 3 sandbox cases.

Verified locally (offline; final package run):
- `python -m pytest -q`: **71 passed, 1 skipped**.
- `python -m compileall -q src tests`: passed.
- `node --check` on extracted dashboard JavaScript: passed.
- Compose YAML parse, 10 expected services and exchange-worker-only sandbox credential injection: passed.
- ZIP archive integrity: checked during release packaging.

- Signed REST request bytes and pinned non-production endpoints.
- Rejection of production endpoint identifiers and live enablement.
- Exact preservation of unknown delivery after a timeout or exchange 5xx.
- Unit risk checks for Spot inventory, lot/tick, size, allowlist, balance and Futures one-way/isolated/leverage/margin/conditional reduce-only.
- Correct Spot regular / Futures Algo REST methods, paths and client IDs.
- Fake-exchange restart/reconcile failure paths: orphan orders, unknown orders, disarmed worker, valid order and timeout incident.
- Syntax validation and Compose YAML parsing (not a Docker image runtime check).

**Not independently executed:** `ruff check` (tool unavailable in this environment), Docker Compose engine, PostgreSQL SQL migration and row-lock contention with a live server, credential-backed Binance Spot Testnet, USDⓈ-M Futures Demo user-data streams, real testnet order placement, server crash drill or funding accounting. These must be verified on the target machine; code coverage is not proof of exchange interoperability.
