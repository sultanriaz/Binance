# AegisTrade — Modules 1–6 integrated

## Verified local Windows run (2026-10-28)

This repository was assembled from the cumulative Module 6 ZIP. The default Compose project is `aegistrade-module2` and uses named volumes `aegistrade-module2_postgres_data`, `aegistrade-module2_redis_data`, and `aegistrade-module2_nats_data`. The development stack collects public **spot** data and supports virtual paper trading. `.env` has generated local database and JWT secrets, `MARKETS=spot`, `ENABLE_LIVE_TRADING=false`, and `ENABLE_TESTNET_EXECUTION=false`. The sandbox exchange worker is behind an opt-in `sandbox` profile. The production overlay and live worker are not started.

From PowerShell in `C:\black_paper\Projects\binance-ai-trader`:

```powershell
docker compose config --quiet
docker compose up --build -d
docker compose ps
curl.exe http://127.0.0.1:8000/healthz
curl.exe http://127.0.0.1:8000/readyz
```

Dashboard: **http://127.0.0.1:8000/**. Prometheus: **http://127.0.0.1:9090/**. The original dashboard is at **http://127.0.0.1:8000/legacy**. PostgreSQL, Redis, and NATS are internal to the Compose network. Normal shutdown retains all named volumes:

```powershell
docker compose stop
# Resume later:
docker compose up -d
```

To enroll your own operator, use an interactive terminal. This command displays a private MFA enrollment URI; scan it into your authenticator and keep it private. Each operator needs a separate account and authenticator for independent approvals. The temporary verification identities used during assembly were disabled after testing.

```powershell
docker compose exec -it gateway python -m trader.bootstrap_admin
```

For local tests, use Python 3.11 or later; Docker images use Python 3.12. The validated host here used Python 3.13:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev,research]'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src
node --check web/terminal.js
```

Troubleshoot with `docker compose ps -a`, `docker compose logs --tail=100 gateway collector outbox paper-worker research-worker operations-worker nats`, and `docker compose exec -T postgres psql -U trader -d trader -Atc "SELECT version FROM schema_migrations ORDER BY version"`. The `migrate` container normally exits with status 0. If public Binance connectivity is unavailable, the dashboard still loads but quote-dependent paper orders fail closed. The included example candles are synthetic; use the checksum-verified history loader for offline replay. Never run `docker compose down -v` on a database you need to retain. See [project state](docs/PROJECT_STATE.md) and [known issues](docs/KNOWN_ISSUES.md) for tested scope.

The sections below retain module-specific architecture and examples from the original deliveries. They are historical context; the verified local commands above describe this installation.

**Module 4 adds a responsive operator terminal, two-person sandbox order approval, a quantitative model laboratory, campaign-driven retraining and approved SHADOW-only model promotion.** It includes complete source carried forward from Modules 1–3 (market monitoring, virtual portfolios/backtesting, Spot Testnet and Futures Demo adapters). **NO PRODUCTION BINANCE ORDER EXECUTION. NO GUARANTEED PROFITABILITY.**

Read [Module 4 architecture](docs/MODULE4_ARCHITECTURE.md), [acceptance](docs/MODULE4_ACCEPTANCE.md), and [verification](docs/MODULE4_VERIFICATION.md). These explain exactly which scenarios remain unverified in Docker.

## Implemented phases

- **P20** — Responsive research/trading terminal: candlesticks + volume, live quote age, risk controls, portfolios, sandbox and model views. `web/index.html`, `web/terminal.js`, `src/trader/terminal.py`.
- **P21** — Dual-control approvals: distinct ADMIN and TOTP, immutable payload hashing, short expiration, single-use atomic approval consumption before sandbox enqueue. `src/trader/governance/` and modified `src/trader/exchange/{http_api,repository}.py`.
- **P22** — Point-in-time closed-candle features, basic regime detection, data leakage and historical continuity checks. `src/trader/intelligence/features.py`.
- **P23** — CPU-based logistic-regression quantitative research, chronological purged splits, after-friction holdout tests, JSON model registry and safe probability inference. `src/trader/intelligence/{training,model,repository}.py`.
- **P24** — Dedicated research-worker, bounded jobs, disabled-by-default recurring challenger campaigns, independent two-person model approval, shadow-only scoring of unseen closed bars and rollback/retirement. `src/trader/intelligence/{worker,http_api}.py`.

## Requirements

- Docker Desktop with WSL2 / Linux containers and Docker Compose
- Internet access to Binance public markets (for live public feeds, depending on region)
- Python ≥3.11, Node (optional, for `node --check`), `git`
- Secure `.env` with `JWT_SECRET` and `POSTGRES_PASSWORD`, configured before first launch
- Two separate administrator accounts and authenticators for independent approvals
- Binance sandbox credentials **optional**, kept only in `.env.exchange` (never in Git)

## Original Module 4 bootstrap example (historical)

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
$secret = python -c "import secrets; print(secrets.token_urlsafe(48))"
(Get-Content .env) -replace '^JWT_SECRET=.*$', "JWT_SECRET=$secret" | Set-Content .env
# Set a strong POSTGRES_PASSWORD in .env BEFORE creating the DB volume.
docker compose config --quiet
docker compose up --build -d
docker compose ps
docker compose logs --tail=100 migrate gateway research-worker
# Enroll first ADMIN:
docker compose exec -it gateway python -m trader.bootstrap_admin
# Enroll SECOND, independently operated ADMIN for dual-control workflow:
docker compose exec -it gateway python -m trader.bootstrap_admin
```

Open **http://localhost:8000** (new Module 4 terminal). Module 3's original console remains at **http://localhost:8000/legacy**. Sign in with a registered account's password and authenticator TOTP.

### Upgrading from Module 3 without losing data

1. **Back up your PostgreSQL volume first.** Verify restore before upgrading; do not run `docker compose down -v`.
2. Extract this source over a separate working copy and preserve **the Compose project name** `aegistrade-module2`; it points to the same existing volume by design.
3. Copy your local `.env` values; do **not** commit or ship private exchange API keys. If you use exchange test credentials, copy them to a separate local `.env.exchange` from `.env.exchange.example`. This file is optional when sandbox execution is disabled, and required only when activating test accounts.
4. `docker compose up --build -d`. The migrate one-shot job now applies `004_intelligence.sql` after `001_init.sql` to `003_exchange.sql`. Existing paper/exchange rows should remain.
5. Run integration/restore/approval tests in [acceptance checklist](docs/MODULE4_ACCEPTANCE.md) before marking the migration accepted.

## Market-data preparation for charts and ML

Historical dataset retrieval needs interval-aligned Unix millisecond timestamps and stores SHA256-verified JSONL:

```powershell
python -m pip install -e ".[dev,research]"
python -m trader.paper.history download `
  --market spot --symbol BTCUSDT --interval 1m `
  --start-ms 1735689600000 --end-ms 1735862400000 `
  --output data/historical/btcusdt_spot_1m.jsonl

docker compose exec gateway python -m trader.paper.history load --file /app/data/historical/btcusdt_spot_1m.jsonl
```

The `data` directory is mounted read-only into the gateway, which is correct for importing datasets into PostgreSQL. Dates above are historical UTC examples; choose a verified closed period. **At least 350 contiguous, closed bars** are required for an ML experiment; 500–2,000 is preferable for laboratory exercises, and no result should be assumed to generalize to live markets.

For a synthetic offline exercise (never for trading decisions):

```powershell
python -m trader.paper.demo --output data/examples/synthetic_btcusdt_1m.jsonl
```

## Controlled workflow

1. Import verified historical bars, sign in, and select **Trading Terminal** to see the chart; the bid price is fetched separately from the actual public live market feed.
2. Select **AI Research Lab** and queue a model experiment (or create a disabled recurring campaign, then deliberately enable it). Monitor job state and model validation metrics.
3. A quality-pass candidate can request SHADOW promotion. A second ADMIN logs in and approves it with fresh MFA in **Approvals**. The first admin or a second authorized operator explicitly activates the SHADOW model.
4. Import new, later closed candles; the research-worker observes unseen bars after the model's training cutoff. Shadow predictions are advisory only.
5. In **Binance Sandbox**, prepare a typed test order. It starts as an **approval request**. Another independent ADMIN approves. Return to Approvals and click **Enqueue on TEST exchange**; the Module 3 exchange worker will still enforce its own account arming, sandbox flags and preflight policies.
6. Pause paper activity, disarm a sandbox account, disable campaigns, or retire shadow models at any time. Existing real exchange positions are never claimed to have been closed by a pause.

**Design invariant:** There is no route from research-model output to the paper or testnet execution engines. Every sandbox order must be requested and independently approved; the model lab cannot generate orders. Production order endpoints do not exist.

## Verify locally

```powershell
python -m pip install -e ".[dev,research]"
python -m pytest -q
python -m compileall -q src
node --check web/terminal.js
docker compose config --quiet
```

### Docker health and integration

```powershell
docker compose ps
docker compose logs --tail=100 migrate gateway research-worker paper-worker exchange-worker
curl.exe http://localhost:8000/healthz
curl.exe http://localhost:8000/readyz
```

If you have a preexisting Module 3 DB, verify the new tables and your previous data. Test a 2-person MFA approval with the sandbox worker **disabled** first; it should create a durable approval and queued intent only after authorization, with no order sent to Binance. Test an approval's replay/expiry, restart services, and confirm the audit remains intact.

## Endpoint summary

- `GET /v1/terminal/candles` — imported, closed chart bars and regime.
- `POST /v1/governance/sandbox-orders` — request a sandbox order approval.
- `GET /v1/governance/requests`; `POST /v1/governance/requests/{id}/review` — dual-control with TOTP.
- `POST /v1/testnet/orders?approval_id=<UUID>` — consumed approved request + sandbox intent only.
- `POST /v1/research/jobs`, `GET /v1/research/jobs`, `GET /v1/research/models` — research runs and registry.
- `POST /v1/research/campaigns`, `GET /v1/research/campaigns`, `POST /v1/research/campaigns/{id}/enabled` — recurring challenger training.
- `POST /v1/research/models/{id}/request-shadow`, `POST /v1/governance/requests/{id}/activate-shadow` — manual gated promotion.
- `GET /v1/research/models/{id}/shadow`, `GET /v1/research/models/{id}/forecast`, `GET /v1/research/models/{id}/performance`, `POST /v1/research/models/{id}/retire-shadow` — monitor and retire shadow models.

All sensitive API routes require a valid admin session. See individual source route handlers for typed request shapes and HTTP errors.

## Safety limitations

- Implemented and offline unit tested, **NOT production/real Binance/Docker integration certified**.
- No self-directed live orders, guaranteed win rate, predictive accuracy guarantee or live account support.
- Model research currently uses one holdout with fixed friction assumptions; it lacks full historical order-book execution, independent market-regime analysis, multi-fold validation and external audit.
- Shared PostgreSQL credentials for gateway and research worker are a production privilege-separation gap. Before live readiness, introduce dedicated roles and database permissions, load testing, dependency scans, WAF/network restrictions and professional security review.
- Browser terminal is a responsive no-build JavaScript SPA (not Next.js); TradingView licenses/API not required for its self-rendered historical chart.

## File locations

- `docs/MODULE4_ARCHITECTURE.md` — comprehensive architecture, data flow and assumptions.
- `docs/MODULE4_ACCEPTANCE.md` — runtime acceptance checklist.
- `docs/MODULE4_VERIFICATION.md` — verified versus pending checks.
- `infrastructure/migrations/004_intelligence.sql` — additive schema.
- `src/trader/intelligence/`, `src/trader/governance/` — Module 4 backend.
- `web/` — modern terminal and preserved Module 3 legacy UI.

---

# Module 5 (P25–P28) — Security, resilience and shadow operations

Module 5 is included **in full**, not as a patch-only archive. It builds on Modules 1–4 while maintaining a strict separation between paper trading, Binance sandbox execution and read-only AI research. **Live-money Binance execution remains disabled.**

What changed: staging security middleware, MFA single-use protection, individual PostgreSQL service roles, Docker secret exposure reduction, finalized Binance candle materialization, Prometheus operational metrics/alerts, immutable shadow-readiness evidence, an operations worker and dashboard, encrypted backup/restore drill, negative/fault tests, and release documentation.

Start with the [Module 5 architecture](docs/MODULE5_ARCHITECTURE.md), [acceptance gates](docs/MODULE5_ACCEPTANCE.md), [operator runbook](docs/MODULE5_RUNBOOK.md) and [verification record](docs/MODULE5_VERIFICATION.md). The next phase **P29** (supervised real spot) is *not implemented or enabled* here.

## Offline verification

```bash
python -m pip install -e '.[dev,research]'
python -m pytest -q
PYTHONPATH=src python scripts/ops/fault_drill.py
python -m compileall -q src
node --check web/terminal.js
```

## Local run

```bash
cp .env.example .env  # then configure JWT_SECRET and POSTGRES_PASSWORD
# If upgrading from Module 4, back up the old PostgreSQL volume first.
docker compose up --build -d
docker compose ps
```

Staging is separately configured through `infrastructure/staging/docker-compose.staging.yml`; see the runbook. Neither backend security claims nor operational acceptance can be inferred from offline tests alone.

---

## Module 6 (P29–P32): controlled live trading engineering release candidate

See [Module 6 architecture](docs/MODULE6_ARCHITECTURE.md), [Module 6 runbook](docs/MODULE6_RUNBOOK.md), [Module 6 acceptance ledger](docs/MODULE6_ACCEPTANCE.md) and [Module 6 release notes](RELEASE_NOTES_MODULE6.md).

The new source contains real-money Binance API capabilities, but **does not start live order submission by default**. The dedicated `live-worker` exists only in the opt-in production Docker profile. It cannot run without the production-only acknowledgment, independent P28 readiness, matching API-key fingerprint, user authorization and risk checks. Live API keys go exclusively in `.env.live` and are never distributed to the API, sandbox or AI workers.

**This ZIP is NOT approved for real-money use as delivered.** Live end-to-end transaction and reconciliation tests, incident recovery, PostgreSQL role checks, TLS/production deployment, eligible Binance accounts and independent review remain required. Start with normal development/paper/sandbox flows, close P25–P28 acceptance gaps, and follow the live runbook before considering any capital exposure. Do not enable futures while its non-atomic stop-loss exposure remains unacceptable.
