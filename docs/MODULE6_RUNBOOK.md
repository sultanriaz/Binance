# Module 6 deployment, release gates and incident runbook

## Safety first

**Never paste exchange credentials into chat, GitHub, issue trackers, `.env.production`, or the browser.** Credentials exist in `.env.live`, readable only by the isolated live-worker container. Disable API withdrawal privileges and use the narrowest available Spot/Futures permissions and allowed IP addresses. Use a dedicated, small-balance exchange account. This repository has not executed even one live exchange order during development here. Do not enable live mode until P25–P28 runtime acceptance and a separate engineering/security review are complete.

## Upgrade from Module 5 — preserve data

1. Perform and verify a fully restorable encrypted PostgreSQL backup of the Module 5 deployment. Test restore in a disposable database.
2. Stop current application containers without removing named Docker volumes. **Do not run `docker compose down -v`.**
3. Unpack Module 6 over a clean copy of the previous repository after comparing migrations and configuration. In a real team use reviewed Git commits rather than directly replacing production source.
4. Apply `006_live.sql` through the migration job; confirm new tables and roles before starting workers. Observe `schema_migrations` and existing 001–005 data.
5. Provision least-privilege database roles with `scripts/ops/provision_roles.py`. New `trader_live` receives only narrowly scoped live state, operational evidence, instruments, and historical bar access. Confirm the privileged password is NOT exposed to the API or live-worker.
6. Ensure existing P28 readiness evidence and active operations alarms are accurate. Resolve existing Module 1–5 runtime acceptance gaps.

## Configuration

```bash
cp .env.production.example .env.production
cp .env.live.example .env.live
chmod 600 .env.production .env.live
```

Set unique strong production service and PostgreSQL role passwords; choose a real ACME-capable `PRODUCTION_HOST`. Keep the gateway's base `ENABLE_LIVE_TRADING=false` flag, as it must NEVER be a credential-bearing trading process. Production Compose overrides the gateway role and sets `LIVE_CONTROL_ENABLED=true` for approved human controls.

Generate the API key fingerprint *locally*, without exposing the secret:

```bash
python -c 'import hashlib,getpass; print(hashlib.sha256(getpass.getpass("API key (not secret): ").encode()).hexdigest())'
```

Copy the fingerprint to `LIVE_SPOT_KEY_SHA256` or `LIVE_FUTURES_KEY_SHA256` in `.env.live`. Use separate spot/futures keys. Production endpoint URLs are pinned in source code. Real credentials **must not** be used with testnet hosts, and vice versa. Keep `LIVE_TRADING_ENABLED=false` until an independent release authorization.

Validate the full production overlay in an environment with Docker Compose:

```bash
docker compose -f docker-compose.yml -f infrastructure/production/docker-compose.production.yml --env-file .env.production --profile live config --quiet
```

Start production infrastructure without live worker first (omit `--profile live`). Do not expose raw PostgreSQL, Redis, NATS or Prometheus ports. Caddy must issue valid TLS certificates and proxy the restricted gateway.

```bash
docker compose -f docker-compose.yml -f infrastructure/production/docker-compose.production.yml --env-file .env.production up -d --build
```

**Live worker enablement is an explicitly separate release operation** with `--profile live` and the exact authorization phrase in `.env.production`, after verified local and staging checks. The phrase intentionally requires `LIVE_TRADING_CONFIRM=I_AUTHORIZE_CONTROLLED_REAL_MONEY_TRADING`. A start with `LIVE_TRADING_ENABLED=false` exits without sending production API requests. Never automate this enablement through an AI agent.

## First release (P29)

1. Confirm `GET /v1/operations/readiness` reports a recent `REVIEW_REQUIRED` with zero unresolved reasons. Remember this means *review*, not automatic GO.
2. Obtain written authorization from an accountable human operator for limited real spot use. Confirm account eligibility and segregated API credentials.
3. Reconcile live account in worker while unarmed. Verify no unmanaged open orders, unknown statuses, unsupported inventory or account positions.
4. Sign in with ADMIN A (MFA), create `SPOT_ARM` approval. ADMIN B reviews and approves with their own MFA. ADMIN A consumes with a **new** MFA token. These are short-lived and single use.
5. Create a `SPOT_ORDER` request with exact client ID, side, quantity, instrument and order type. ADMIN B approves; ADMIN A explicitly consumes. Worker preflights again against current real exchange state and submits one small canary order. Monitor actual exchange order/fill and local reconciliation.
6. Independently inspect the account in Binance. If any discrepancy occurs, disable new entries and enter incident mode; do not automatically retry uncertain orders.

P30 automation follows P29's successful supervised operations and a separate `SPOT_AUTOMATION` approval. No live AI model is allowed to direct orders. Even approved automation can be stopped with `POST /v1/live/accounts/spot-live/halt` without MFA after login; this stops new entries **but does not close existing positions**.

## Futures (P31) — separate high-risk approval

Verify the actual account's regional futures eligibility, permitted API access, one-way mode, isolated margin and leverage <=2. Obtain a second independent futures release approval. Every opening futures order must carry its own protective stop price. The entry is submitted before its stop, causing a non-atomic unprotected interval. **If this risk is unacceptable, do not enable live futures.** The implementation does not provide fully autonomous futures risk protection under network partitions.

Futures canary must validate actual position after each entry, existence and acceptance of an Algo STOP_MARKET reduce-only order, correct stop orientation and size, and accurate funding/fee accounting. A rejected or unknown protection must trigger an immediate operational incident requiring manual action in the Binance interface. There is no safe blanket recommendation to blindly market-close after uncertain order outcomes.

## P32 emergency procedures

- **Suspected duplicate, stale account snapshot, or unknown order**: immediately halt new entries, consult Binance account/order history and exchange fills, reconcile by stable client order ID, and involve an authorized human. Never replay a timed-out POST.
- **Futures position without accepted protection**: treat as CRITICAL. Inspect live margin/position and independently execute an authorized protective/reduction intervention in Binance. Server inability to cancel orders does not equal closure.
- **Network outage/DB outage**: halt automation and new entries; maintain independent real-account monitoring and notifications. Restart does not automatically arm accounts.
- **Unexpected balances/open orders**: suspend automation and investigate unmanaged activity; do not sweep or delete records to satisfy a readiness gate.
- **Credential compromise**: suspend permissions, rotate/revoke Binance keys, check withdrawals are disabled, reconcile all activity and preserve forensic evidence.
- **Model degradation**: retire model from shadow; it should never have direct live trading permission.

## Operational acceptance that cannot be completed offline

- Full Docker production overlay, migration/role grants and upgrade/rollback from an actual Module 5 database.
- Two ADMIN logins, independent MFA approvals, single-use SQL consumption and concurrent decision races on live PostgreSQL.
- Real Binance live account permissions, rules, server time and signed REST compatibility.
- Network-disconnect and timeout injection against a controllable exchange sandbox and live-compatible protocol.
- Cross-check every actual fill, commission, fee currency, funding, position and Algo stop against Binance; recover after container kill and restart.
- Prometheus alerts delivered to on-call notification channels, incident response drill, independent backup restoration and an approved release evidence record.
- Sufficient shadow/paper runtime observation across market regimes. A training score cannot establish profitability.

**Release decision: NO_GO for use with actual capital until the above evidence is signed off.**
