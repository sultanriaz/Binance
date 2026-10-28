# Low-Level Design — contracts, data and state

## MarketEvent envelope

```json
{
  "event_id": "sha256hex",
  "market": "spot",
  "symbol": "BTCUSDT",
  "event_type": "agg_trade",
  "exchange_ts": 1672515782136,
  "received_ts": 1672515782155,
  "payload": {"price": "120.20", "quantity": "0.12", "trade_id": 200, "trade_ts": 1672515782130}
}
```

`event_id` is SHA-256 of canonical `{market,symbol,kind,ts,payload}`; it is invariant under duplicate reception, including after reconnect. `received_ts` is excluded from the fingerprint. The event ID is **not** a Binance-issued identifier and does not prove that no observations were lost.

## Supported event types
- `agg_trade` from `<symbol>@aggTrade`.
- `kline` from `<symbol>@kline_1m`; carries whether Binance marks the candle closed.
- `book_ticker` from `<symbol>@bookTicker`; contains best bid/ask, quantity and exchange update ID, but not a complete depth book.
- `mark_price` from futures `<symbol>@markPrice@1s`; contains mark, index, funding estimate and next funding timestamp.

## Stream processing
1. Build separate Spot/Futures combined WebSocket URL with an explicit configuration whitelist of symbols.
2. Connect and receive Binance event object; validate symbol and supported event type.
3. Enqueue onto bounded in-memory queue of 5,000. Overflow closes the current stream and triggers reconnect rather than silently dropping data.
4. Transactionally insert history row (`ON CONFLICT DO NOTHING`), latest row and outbox row. Duplicate event IDs are skipped.
5. For aggregate trades, persist last trade ID; if a new sequential ID skips values, record an unresolved gap in `market_data_gaps`. On restart this high-water mark is retained.
6. Outbox service publishes in creation order using `Nats-Msg-Id`; only after JetStream ACK marks event published.
7. Dashboards query PostgreSQL via authenticated gateway. `age_ms` and `stale` are derived from receipt timestamp, not assumed from exchange timestamp.

## Idempotency and delivery
- Database event insert idempotency: PK `event_id`.
- Outbox insert idempotency: unique `event_id`.
- NATS publishing: at-least-once. Consumer must deduplicate by `event_id` (JetStream's deduplication window is not unlimited).
- Events are not guaranteed exactly-once end to end; connection loss can create historical gaps.

## API security
- CLI account bootstrap only, no anonymous self-registration.
- Password: `hashlib.scrypt`, random 128-bit salt.
- MFA: RFC 6238 TOTP with ±1 step drift window.
- Login throttling: Redis key scoped to hashed client IP + username, max 8 attempts in 5 minutes; fails closed when Redis unavailable.
- JWT: HMAC SHA-256, 15 min TTL; every protected API request checks current user and role against DB.
- No JWT refresh or token revocation feature in D1; tokens remain usable for up to 15 minutes except if account is disabled or role changes.

## Health and audit
- `/healthz` confirms process alive, not data freshness.
- `/readyz` tests database availability, not Binance reachability.
- `/v1/market/streams` reports collector stream connectivity and 30s event freshness independently.
- `/v1/market/data-quality` records detectable unresolved aggregate sequence anomalies; absence of anomalies is NOT proof of completeness.
- `audit_logs` retains auth enrollment/login attempts and outcomes (no passwords or TOTP digits).

## Extensibility boundary for D2
Future paper trading must consume only permitted, freshness-checked data via a validated contract and must implement its own deterministic risk and ledger. Never interpret `book_ticker` as guaranteed executable volume or a complete L2 order book.
