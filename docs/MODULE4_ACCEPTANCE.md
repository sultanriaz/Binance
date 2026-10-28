# Module 4 — Required acceptance gates

Status: **IMPLEMENTED; OFFLINE VERIFIED; LIVE DOCKER/DB/SANDBOX ACCEPTANCE PENDING**.

| Phase | Gate | Evidence / command |
|---|---|---|
| P20 | Operator logs in with MFA, chooses spot/futures, imported closed charts render, stale quotes labeled, mobile layout usable. | Run `docker compose up --build -d`; import history; open http://localhost:8000. |
| P21 | Two DISTINCT ADMIN accounts; requester creates sandbox order approval; same-user approval denied; second user's correct TOTP required; expired or modified payload denied; double consumption rejected. | UI and `POST /v1/governance/requests/{id}/review` + `POST /v1/testnet/orders?approval_id=`. PostgreSQL transaction fault tests needed. |
| P22 | Features do not change when FUTURE candles change; labels use next-open entry and future close only; data is contiguous. | `pytest tests/test_intelligence_module4.py` |
| P23 | A bounded experiment trains on ≥350 closed imported bars; worker generates a checksummed JSON-only model; scaler train-only; candidate metrics persisted. | Start `research-worker`, submit job, inspect `/v1/research/jobs` and `/v1/research/models`. |
| P24 | New campaign disabled by default; enabling causes job after sufficient NEW bars; no automatic order submission; shadow promotion requires quality gate and second admin; newly imported bars are scored strictly after training cutoff; retirement stops new predictions. | Enable campaign; import additional history; observe research-worker and `/v1/research/models/{id}/shadow`; review governance events. |
| Compatibility | Original Module 1–3 regression tests pass; production trading stays disabled, test environment keys exclusive to exchange-worker; no secrets in ZIP. | `pytest -q`, `node --check web/terminal.js`, `docker compose config --quiet`, `grep` controls. |

## Additional independent runtime checks before sign-off

1. Backup the existing PostgreSQL volume and verify restore with an isolated Compose deployment.
2. Confirm `004_intelligence.sql` applies cleanly and repeatably to **existing** Module 3 data.
3. Verify dashboard rendering with real authenticated API and several mobile viewport sizes; test keyboard navigation and screen reader labels.
4. Test simultaneous approvals and enqueue requests in separate DB sessions; no double consume or double economic-intent submission.
5. Test malicious payload tampering, missing approval, same-admin review, replayed consumed request, expiry, bad TOTP and role mismatch.
6. Kill research-worker mid-training; old jobs fail explicitly and do not auto-deploy. Resume worker and create a new job.
7. Verify candles are historical and stale labels are correct; no accidental treatment of incomplete bars as decision input.
8. Use Spot Testnet and Futures Demo credentials ONLY after completing all Module 3 tests, with both environment flags set and account armed.
9. Verify no `api.binance.com` or live `fapi.binance.com` order paths in execution adapter; Module 3 must still pin sandbox endpoints.
10. Measure research training resource consumption, alerting, and DB privileges; split out dedicated read/write database roles before operational release.
