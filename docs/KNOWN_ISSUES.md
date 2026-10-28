# Known issues and unverified gates

As observed during the local integration on 2026-10-28 (Asia/Karachi):

1. Browser automation was unavailable. Dashboard HTML, JavaScript, authenticated API endpoints, and Prometheus targets loaded, but visual chart rendering and interaction were not inspected in a browser.
2. The bundled BTCUSDT example candles are synthetic and years older than the current public BTCUSDT candle. A naive latest-100-bar replay crosses that gap and correctly fails continuity checks. The isolated `SYNTHUSDT` synthetic dataset was used for the successful offline backtest. Do not use synthetic bars for trading decisions.
3. No sandbox account credentials were configured. Testnet execution remains disabled and the `exchange-worker` is excluded from the default Compose profile. Credential-backed sandbox fill/reconciliation, signed streams, and recovery are unverified. No testnet order was sent.
4. No live exchange account, order, fill, or protective stop was tested. Live accounts remain halted and unarmed; the production overlay and live worker were not started. Futures stop placement is not atomic with entry and remains a release blocker.
5. Continuous shadow observation, independent P28 acceptance, backup and restore drill, staging role verification, TLS deployment, load testing, and independent security and accounting reviews remain pending. The development stack uses the shared `trader` PostgreSQL identity; staging privilege separation was not validated here.
6. Public Binance connectivity is region and network dependent. If the feed becomes stale or unavailable, quote-dependent paper orders reject. A successful spot connection during this run does not establish ongoing data completeness.

The platform is **NO_GO for real-money trading**. See `MODULE6_ACCEPTANCE.md` and `MODULE6_RUNBOOK.md` for the remaining formal gates.
