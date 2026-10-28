# Module 5 — acceptance checklist

`IMPLEMENTED` is not `VERIFIED` and is not `ACCEPTED`. Runtime acceptance must be based on observed results, not architectural intent.

## P25 — Security

- [ ] Docker build works from fresh clone and authenticates with two independent administrators.
- [ ] Staging Caddy issues valid TLS for the configured domain and rejects non-allowlisted hosts.
- [ ] Gateway and research containers cannot inspect `.env.exchange` or retrieve Binance keys.
- [ ] Research role `trader_research` cannot `INSERT` exchange order intents, approvals or audit attestations (database-enforced).
- [ ] Staging authentication rate limit and consumed MFA replay fail closed with Redis outage.
- [ ] Independent vulnerability assessment completed; no unresolved critical findings.
- [ ] Live credentials unavailable to every container; `ENABLE_LIVE_TRADING=true` prevents startup.

## P26 — Reliability

- [x] Offline fault test script covers 10 failure modes and requires `NO_GO` for each (source implementation).
- [x] Regression test suite covers deterministic risk, paper account invariants, exchange status and governance.
- [ ] Fault injection on running staging DB, Redis, NATS and Binance *test* endpoints demonstrates safe behavior.
- [ ] Interrupted order submissions are reconciled; never blindly resubmitted.
- [ ] Stream disconnect/restart and candle gaps block shadow/strategy activity.
- [ ] Enforce correct policy on unknown/cancel-in-flight order and incident states.

## P27 — Staging and disaster recovery

- [ ] Migration 005 applies idempotently to the existing Module 4 volume.
- [ ] Staging overlay passes `docker compose config --quiet` with real interpolation.
- [ ] Real TLS site, Prometheus alerts and off-host notification receiver verified.
- [ ] Encrypted PostgreSQL backup/decrypt/restoration performed to a disposable DB.
- [ ] Backup and readiness evidence cannot be mutated by unprivileged DB users.
- [ ] `scripts/ops/verify_db_runtime.sql` passes under database owner.
- [ ] Simulated full-restore DR runbook checked and recovery-time measurements recorded.

## P28 — Shadow observation

- [x] Readiness evaluator and incident persistence implemented.
- [x] Negative-case tests verify fail-closed behavior for missing/invalid data.
- [ ] Continuous imported closed bars and research-worker shadow observations verified.
- [ ] ≥14-day genuine shadow period and ≥100 resolved observations measured per model.
- [ ] Model quality thresholds satisfied with justified trading-cost assumptions and benchmarks.
- [ ] Operations evidence SHA256 chain verified from genesis and externally archived.
- [ ] Independent operator signs GO/NO-GO risk assessment; `REVIEW_REQUIRED` not interpreted as trade authorization.

**Current status:** Code complete; **offline test verification** complete; P25–P28 **runtime acceptance pending**. Live-money execution disabled and not included.
