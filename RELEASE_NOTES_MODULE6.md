# Module 6 release candidate — P29–P32

Integrated Modules 1–6 into a single repository with production trading capability *disabled by default*.

New: `trader.live` package, `006_live.sql`, production Compose overlay, second-admin authorizations, real order intent and fill audit, market-data-replayed SMA spot strategy, small canary risk ceilings, futures stop plan, operational reporting, incident alerts and live-operator dashboard.

**This is an engineering release candidate, not a fully qualified financial trading system.** Source-level tests and static checks are not sufficient for a production GO. No real-money or Binance-authenticated test has been conducted by this implementation session. Futures opening and stop placement are not atomic. Detailed deficiencies are in `docs/MODULE6_ARCHITECTURE.md`, `docs/MODULE6_ACCEPTANCE.md`, `docs/MODULE6_RUNBOOK.md`.
