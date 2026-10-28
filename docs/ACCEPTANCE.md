# Acceptance gates — Division 0 and Division 1

Status notation: `CODE_COMPLETE` means artifacts exist; `LOCALLY_TESTED` means isolated checks passed; `RUNTIME_UNVERIFIED` means the actual Docker/network environment has not been exercised; `NOT_ACCEPTED` means the formal phase gate remains open.

| Phase | Evidence in archive | Local status | Remaining acceptance |
|---|---|---|---|
| P00 Product scope | PRD.md, SECURITY.md, ROADMAP.md | CODE_COMPLETE | Stakeholder review and exchange/jurisdiction eligibility |
| P01 Repository init | Monorepo structure, README, .gitignore | CODE_COMPLETE | Create private remote, push, set branch protection |
| P02 HLD/LLD | HLD.md, LLD.md, ADR-001.md, schema SQL | CODE_COMPLETE | Independent architecture review |
| P03 CI/CD | .github/workflows/ci.yml, Makefile, pyproject.toml | LOCALLY_TESTED (tests) | GitHub Actions execution on remote |
| P04 Runtime | Dockerfile, compose, volumes, health checks | RUNTIME_UNVERIFIED | `docker compose up --build`, restart and persistence |
| P05 DB/events | 001_init.sql, Storage, transactional outbox | CODE_COMPLETE | Real PostgreSQL transaction and JetStream integration |
| P06 Auth/config | security.py, API, CLI enrollment, fail-closed settings | LOCALLY_TESTED (auth primitive tests) | Login/MFA and Redis throttling in Docker |
| P07 Binance REST | Spot/Futures exchangeInfo/time client | LOCALLY_TESTED (mock REST) | Live endpoint compatibility and regional reachability |
| P08 Market streams | Combined WSS, event normalization, status, cache/outbox | LOCALLY_TESTED (parser only) | Live stream and reconnect test; full depth and auto-backfill deferred |

**Formal Division 1 status: NOT_ACCEPTED** until runtime checks pass. Do not claim MARKET_DATA_READY from static files alone.

## Minimum runtime signoff checklist

- [ ] Docker Compose config validates on target host.
- [ ] Fresh stack reaches ready state and database migrations created expected tables.
- [ ] Admin enrolls and signs in with a real TOTP app.
- [ ] Anonymous API market data requests receive HTTP 401.
- [ ] `ENABLE_LIVE_TRADING=true` prevents service startup.
- [ ] Spot REST metadata and WS produce events from actual Binance public endpoints.
- [ ] Futures REST metadata and WS produce events, including mark price/funding estimates.
- [ ] PostgreSQL stores normalized data with unique event IDs and latest projections.
- [ ] NATS outbox transitions rows to published after JetStream ACK.
- [ ] Kill/restart collector; inspect reconnects, stream state and persisted history.
- [ ] Simulate NATS outage; outbox backlog survives and drains when NATS returns.
- [ ] Simulate Redis outage; market storage persists and login fails closed.
- [ ] Verify stale data indicators after stopping the collector.
- [ ] Check aggregate-trade gap recording when deliberately skipping fixture sequence values.
- [ ] Observe memory, disk usage and retention growth, then set operational limits.

**Do not treat any unchecked item as passing.**
