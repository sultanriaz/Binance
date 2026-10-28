# P09–P14 acceptance and verification matrix

Date: 2026-10-28. See `MODULE2_ARCHITECTURE.md` for scope and invariants.

| Phase | Implemented | Verified here | Blocker for external acceptance |
|---|---|---|---|
| P09 Historical ingestion, replay, dataset integrity | YES | SHA manifest tampering, strict gap detection, pagination, synthetic dataset | Verify Binance public network API and DB import on actual host |
| P10 Deterministic pre-trade risk | YES | Limits, stale quote, pause, spot availability, futures direction, idempotency | Independently review real-market assumptions and risk policy configuration |
| P11 Portfolio/ledger virtual accounting | YES | Decimal spot/futures/funding calculations, state roundtrip, ledger sequence | Test real PostgreSQL transactions, row locking, crash/recovery and multiple worker concurrency |
| P12 Paper order execution | YES | Market/limit/conditional, top-book participation, partial fills, simplified liquidation | Validate long-running worker on real best-quote stream, external simulator model fidelity |
| P13 Baseline automation | YES | SMA pure logic and deterministic shared engine | Verify 1m candle-to-quote to strategy-to-DB transaction end to end, 24h observation |
| P14 Backtesting and analytics | YES | next-bar chronology, costs, reported return/drawdown/approx Sharpe, repeatability | Broader backtest reliability and statistical validity evaluation on verified real datasets |

**Overall: implementation candidate; NOT independently accepted for full live-market module operation in this authoring environment.** P15–P32 and every real-money capability remain unimplemented.

## Locally executed checks

Commands:

```bash
python -m pytest -q
python -m compileall -q src
PYTHONPATH=src python -m trader.paper.demo --output data/examples/synthetic_btcusdt_1m.jsonl
node --check /tmp/aegis-ui-script.js  # extract inline dashboard script, or use script in README
```

Only reports with exact observed counts belong in `docs/MODULE2_VERIFICATION.md`.

## External acceptance procedure

1. Copy `.env.example` and set random JWT secret; configure strong DB password before first startup.
2. `docker compose up --build -d`; ensure migration exits `0` and `gateway`, `collector`, `paper-worker` run healthy.
3. `docker compose exec gateway python /app/scripts/paper_smoke.py` (scripts mounted read-only into gateway).
4. Verify live market top-of-book and closed 1m events, plus `historical_bars` import.
5. Admin login with TOTP; create paper account and place a small paper spot BUY, SELL, futures short and reduce-only cover.
6. Verify order idempotency, paused account rejects new entries, and conditional stop/take-profit test logic.
7. Enable one SMA paper strategy after recording the risk limits; watch real closed-bar cursor and ledger for at least one test cycle.
8. Replay a trustworthy historical dataset and compare repeat runs; inspect fees, slippage and missing-data errors.
9. Restart `paper-worker`, `gateway` and `postgres`; verify no repeated closed-candle orders and no account/ledger drift.
10. Only after inspecting evidence mark P09–P14 runtime acceptance; otherwise retain blockers in `KNOWN_ISSUES.md`.
