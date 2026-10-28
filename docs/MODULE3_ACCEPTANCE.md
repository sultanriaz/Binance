# Module 3 acceptance matrix — P15–P19

**Execution result as packaged (2026-10-28): CODE COMPLETE FOR DEFINED SANDBOX SCOPE / RUNTIME NOT ACCEPTED.**

Offline suite: 71 passed, 1 skipped. Complete Docker/PostgreSQL/Binance E2E acceptance remains pending, so no full acceptance claim is made.

| Phase | Offline / mocked exit | Docker / Binance integration exit | Status |
|---|---|---|---|
| P15 | Durable states, stable IDs, idempotency, unknown-write test, cancellation transitions and single-claim contract | PostgreSQL migrations, concurrent-worker lock, crash/restart drill | Offline verified; integration pending |
| P16 | Spot REST signing, URL pinning, market/limit/conditional API routes, query/cancel mock and risk checks | Real testnet credential authentication, minimum-order sandbox canary, balances/fills and WebSocket delivery | Offline verified; integration pending |
| P17 | Isolated/one-way/leverage/quantity/filter/margin/risk checks | Real demo margin mode and position snapshot, protective trigger/cancel, funding/position reconciliation | Offline verified; integration pending |
| P18 | USDⓈ-M Futures Demo regular + Algo route tests, user-stream endpoint implementation | Demo REST order → execution reports → Algo order verified → safe cancellation | Offline verified; integration pending |
| P19 | Unknown timeout, orphan open order, disarmed control, successful simulated submit, signed event dedup | Docker restart, DB crash after HTTP write, websocket outage, duplicate deliveries, known-unknown reconciliation, position cleanup | Offline verified; integration pending |

## Operator E2E checklist — cannot skip for acceptance

1. Run with blank sandbox credentials; confirm all exchange writes remain disabled and paper trading continues.
2. Set `ENABLE_TESTNET_EXECUTION=true` and `TESTNET_CONFIRM=SANDBOX_ONLY_I_ACCEPT` in `.env`, test environment keys in `.env.exchange` only.
3. `docker compose up --build -d` and `docker compose ps`; review `docker compose logs -f exchange-worker`.
4. Confirm `readyz`, API authentication, schema migration `003_exchange`, and account states `READY` but `operator_enabled=false`.
5. Arm Spot Testnet. Submit a compliant, small TEST MARKET order; verify an actual order ID in testnet history, DB row, fill(s), fee currency, and account balance. Repeat with same client ID+payload; verify one economic order. Repeat with altered payload; verify HTTP 409.
6. Submit a Spot Testnet LIMIT order away from market; cancel it; verify `CANCEL_PENDING` followed by `CANCELED` with exchange confirmation.
7. Perform supported Spot conditional LIMIT test and verify trigger behavior or cancel it; inspect locked/free balances.
8. Configure USDⓈ-M DEMO for one-way/isolated/low leverage. Arm. Make a small demo opening order. Place an independently verified reduce-only conditional stop via `/fapi/v1/algoOrder` and confirm `openAlgoOrders` visibility; safely cancel/close using separate controlled orders.
9. Artificially fail network response after submission and restart the worker. Verify no new order is sent, original client ID queried, and account requires rearm.
10. Create an external manual TEST order and verify automatic halt for an unmanaged order. Resolve it manually, acknowledge the incident, reconcile, rearm.
11. Disconnect/reconnect Spot and Futures user streams; verify polling still reconciles orders, updates remain idempotent, and stale status does not turn into assumed fill.
12. Test a fresh/clean Docker volume and separately upgrade from Module 2's existing persistent PostgreSQL volume.

**Approval rule:** Do not call P15–P19 fully accepted until all critical scenarios above are actually verified and documented with timestamps, log excerpts, database references and exchange-visible records. Never use real-money credentials for this checklist.
