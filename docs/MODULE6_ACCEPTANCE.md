# Module 6 acceptance ledger (initial)

| ID | Requirement | Current state |
|---|---|---|
| P29-01 | Live spot REST client and typed order contract | IMPLEMENTED; offline validation |
| P29-02 | Separate production key storage and SHA256 binding | IMPLEMENTED; static tests |
| P29-03 | Independent two-admin MFA authorization + one-use approval | IMPLEMENTED; real DB concurrency PENDING |
| P29-04 | Spot precision, balance, notional, spread and drawdown policy | IMPLEMENTED; mock/pure tests; actual account PENDING |
| P29-05 | Submitted, uncertain, filled, cancelled and restored order lifecycle | IMPLEMENTED; real exchange PENDING |
| P30-01 | Independent bounded spot automation approval | IMPLEMENTED; real DB PENDING |
| P30-02 | Closed contiguous candle SMA policy, atomic cursor + enqueue | IMPLEMENTED; full worker PENDING |
| P30-03 | Strategy automation restart/drawdown/failure drill | PARTIAL; full fault integration PENDING |
| P31-01 | Isolated one-way USD-M futures restrictions | IMPLEMENTED; real account PENDING |
| P31-02 | Parent-linked reduce-only Algo stop and post-submission query | IMPLEMENTED; live correctness PENDING |
| P31-03 | Futures protected exposure through outages/partial fills | NOT ACCEPTED: cannot guarantee exchange-atomic protection |
| P32-01 | Production Compose, role isolation, TLS ingress | IMPLEMENTED files; Docker deployment PENDING |
| P32-02 | Live incident metrics, immutable event history, reporting | IMPLEMENTED; DB/alerts PENDING |
| P32-03 | Backup, incident drill, release sign-off, ongoing operations | DOCUMENTED; OPERATOR EXECUTION PENDING |

## Release gate

All P25-P28 runtime failures MUST be closed. Independent security and exchange integration reviewers must sign off before live use. Spot canary (P29) is first; spot automation (P30) and futures (P31) require separate written approval. Never infer trade-readiness solely from unit-test count, a simulation, or a green CI pipeline.
