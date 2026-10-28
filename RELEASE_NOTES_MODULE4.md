# Release notes — Module 4 (P20–P24)

## Delivered

- Integrated Modules 1–3 with Module 4's responsive operator dashboard and closed historical OHLCV charting.
- Server-side request/approval/consume enforcement for sandbox orders, including second ADMIN with TOTP, expiry, approval payload hash, and atomic single-use request consumption.
- Quantitative features and learning experiments using bounded chronological data, a strict purged holdout, train-only preprocessing, separate validation threshold selection, and no executable model deserialization.
- Research job queue, isolated research-worker image, disabled-by-default recurring challenger campaigns and model registry.
- Quality-checked candidate models requiring second-admin approval before becoming SHADOW, with unseen closed bar observation, read-only probabilities, monitoring and manual retirement.
- Additive PostgreSQL migration, CI coverage, code-level tests, architecture, acceptance and operator documentation.

## Compatibility and safety

- Existing paper, public market and exchange testnet state preserved in migrations 001–003.
- New migration 004 adds governance and research structures.
- `POST /v1/testnet/orders` is a BREAKING CHANGE: independent approval ID now required; original `/legacy` form cannot bypass it. Use the new Module 4 approval center.
- Production exchange execution remains disabled. Modules 1–3 runtime acceptance was not previously established and cannot be assumed.
- `Dockerfile.research` installs optional sklearn dependencies; other services don't need these dependencies.
- `.env.exchange` is optional when disabled (Docker Compose v2.24+), and credentials are provided only to exchange-worker.
- No API keys, passwords, live trading credentials or cached Python bytecode included in the release ZIP.

## Known limitations

- Docker, PostgreSQL, two-admin approval integration, and Binance testnet connectivity unverified in current environment.
- Browser app is a no-build JavaScript SPA, not the previously proposed Next.js architecture. Charts use imported candles; current best-bid prices are shown separately.
- No LLM-controlled execution, automatic strategy activation, or model-to-order bridge. No guarantee of predictive performance or profitability.
- Some enterprise-grade requirements (DB role separation, ongoing security review, load tests, comprehensive walk-forward evaluation, real slippage calibration) remain future D5 gates.
