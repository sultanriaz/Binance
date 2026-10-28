# High-Level Design — D0 + D1

## Runtime topology

```text
Binance Public Spot REST/WSS    Binance Public USDⓈ-M REST/WSS
           |                               |
           +------------+------------------+
                        v
              collector (Python)
                        |
            normalized MarketEvent
                        |
              PostgreSQL transaction
              /                 \
         market_events     outbox_events
         market_latest          |
         stream_status          v
         instruments     outbox publisher
              |                 |
              v                 v
  authenticated FastAPI     NATS JetStream
              |
          browser read-only monitor

Redis: optional last-event cache; failed cache updates must not fail PostgreSQL.
Prometheus: gateway and collector metrics; no production alert-routing configuration yet.
```

## Trust boundaries
- Binance public feeds: untrusted network input; strict event shape validation; no signed endpoints.
- Market ingestion: no access to trading credentials; event payloads stored as typed normalized JSON.
- DB: source of truth; all event + outbox inserts transactionally coupled.
- NATS/Redis: derived/ephemeral; must not replace durable state.
- Gateway: bearer token, DB-backed account activity check, MFA at login.
- Browser: receives access token only; does not have access to DB/Redis/Binance credentials.

## Architecture decisions
- **ADR-001:** A read-only Python collector is sufficient for early market data; order manager/risk manager will be Go in later divisions. This is deliberate, not a claim that Python should manage final live orders.
- **ADR-002:** PostgreSQL primary; JSONB market payloads remain reconstructible.
- **ADR-003:** NATS JetStream with transactional outbox to prevent publish-before-commit loss; at-least-once semantics.
- **ADR-004:** Explicit environment fail-closed flag `ENABLE_LIVE_TRADING=false`; no order API implementation.
- **ADR-005:** Book ticker for best bid/ask, not full L2; recorded aggregate-ID gaps, no automated gap repair until P09.

## Dependency boundaries
- P04/P05: Docker, DB, Redis, NATS, migrations.
- P06: operator bootstrap, auth, MFA, API rights.
- P07: metadata clients, market-specific paths.
- P08: WSS, normalized events, store/outbox/monitor.

## Operational failure modes
- DB write failure: collector stops, supervisor restarts; never fabricate successful ingestion.
- NATS outage: committed outbox backlog remains for retry.
- Redis outage: gateway rejects fresh logins if login throttling is unavailable; market store remains authoritative.
- WSS disconnect: reconnect with exponential backoff; aggregate trade gaps flagged if detectable.
- Unknown Binance metadata: errors logged; stored last snapshot may become stale, and is timestamped.
- Frontend connectivity failure: display stale error rather than pretend market is current.
