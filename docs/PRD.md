# PRD — Binance AI Trading Platform: Project Foundations and Module 1

## Objective
Build the first secure, observable, reproducible component of a future AI research, paper trading, and controlled Binance Spot/USDⓈ-M Futures system. This module strictly receives public market data and performs no order entry or trading account administration.

## Target user and scope
Initial use: a single operator configuring the local stack and exploring public market data. ADMIN and VIEWER roles exist. Future multi-user tenancy, order execution and funds management are explicitly excluded until separate requirements and reviews.

## In-scope user stories
1. Operator can bootstrap source via a local Docker Compose stack.
2. Operator can enroll through a CLI with a strong password plus an authenticator code.
3. Authorized users can log in and read fresh public market data for configured symbols.
4. Operator can observe instrument rules for Spot and USDⓈ-M Futures.
5. Operator can see stream disconnects, reconnect counts, and stale/latest observations.
6. Research integration can consume normalized persistent records and NATS events after deduplicating by `event_id`.
7. Operator can inspect recorded gaps in aggregate-trade IDs and review readiness.
8. Project fails startup when accidentally configured with `ENABLE_LIVE_TRADING=true`.

## Nonfunctional requirements
- Prices and quantities must retain exchange-provided string precision; do not convert to binary floats.
- No live keys or order APIs in this module.
- PostgreSQL writes and outbox inserts are one transaction; NATS delivery at least once.
- Disconnect/backpressure behavior visible; unknown data quality cannot be treated as clean.
- Local ports bound to loopback, no direct internet publication.
- Credentials hashed, MFA mandatory, short-lived JWTs and authenticated API reads.
- Instrument metadata refreshed every 6 hours, with startup attempt.
- All environments configured through explicit variables with safe defaults.

## Product milestones
- P00–P03: repo and specifications.
- P04–P08: operating read-only public market-data foundation.
- P09–P14 (future): historical replay, audited risk engine, paper wallet, simulator, strategies, backtesting.
- P15–P19 (future): Binance exchange execution TEST ENVIRONMENTS.
- P20–P24 (future): professional terminal and AI laboratory.
- P25–P32 (future): reliability and approved live rollout.

## Acceptance
See `ACCEPTANCE.md`. Runtime acceptance requires a clean Docker startup, authenticated REST checks, actual Spot/Futures public observations, and restart/reconnection inspection. Unit tests alone cannot constitute full acceptance.
