# Module 3 release notes — P15 to P19

**Version:** 0.3.0 (2026-10-28)  
**Packaging:** Modules 1, 2, and 3 source combined  
**Safety state:** Binance production trading disallowed; Binance sandbox worker disabled by default.

## Delivered

- P15: durable test-only order lifecycle, idempotent intent fingerprints, unknown-status handling, cancellation, PostgreSQL writer locking and crash recovery.
- P16: Spot Testnet signed order submission, status, cancellation, fills, account and signed WebSocket user updates.
- P17: deterministic futures isolated-margin/one-way/leverage/margin/reduce-only preflight; conditional protection and position observation.
- P18: Binance USDⓈ-M Futures Demo ordinary and Algo order routing, user stream, reconciliation endpoints.
- P19: periodic REST-authoritative account/order/fill reconciliation, duplicate event persistence, unmanaged order/position detection, admin acknowledgement/arming and restart fail-closed.
- UI: Admin-only sandbox controls, independent of existing Module 2 paper trading.
- Deployment: additive migration `003_exchange.sql`, separate `exchange-worker` with sandbox credentials, prior Module 2 project name preserved for existing database volume compatibility.

## Local validation

- Unit/mock tests: 71 passed; 1 skipped.
- Python compileall: passed.
- Frontend JavaScript parse with Node: passed.
- Docker Compose YAML parsing and secret-scoping assertions: passed.

## Not independently validated here

- Docker image builds and running PostgreSQL schema migration, database advisory-lock race, Binance test-account authentication, real testnet orders or fill, WebSocket data delivery, actual restart drill, or CI Ruff linting. Full P15–P19 acceptance is pending the integration checklist in `docs/MODULE3_ACCEPTANCE.md`.

## No live money

Never supply Binance production credentials; only Spot Testnet/Futures Demo secrets. `ENABLE_LIVE_TRADING` is intentionally forbidden in Module 3.
