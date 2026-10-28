"""Durable PostgreSQL state and transactional event outbox.

PostgreSQL is authoritative; Redis/NATS are never the source of trading state.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from trader.market import MarketEvent


class Storage:
    def __init__(self, url: str):
        self.url = url
        self.pool = None

    async def open(self) -> None:
        from psycopg_pool import AsyncConnectionPool
        self.pool = AsyncConnectionPool(self.url, min_size=1, max_size=5, open=False,
                                        kwargs={"autocommit": False})
        await self.pool.open(wait=True, timeout=20)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def ping(self) -> bool:
        assert self.pool
        async with self.pool.connection() as conn:
            row = await (await conn.execute("SELECT 1")).fetchone()
            return row == (1,)

    async def write_events(self, events: Iterable[MarketEvent]) -> int:
        from psycopg.types.json import Jsonb
        assert self.pool
        created = 0
        async with self.pool.connection() as conn:
            async with conn.transaction():
                for e in events:
                    data = Jsonb(e.payload)
                    cursor = await conn.execute("""
                        INSERT INTO market_events(event_id,market,symbol,event_type,exchange_ts,received_ts,payload)
                        VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING event_id
                    """, (e.event_id, e.market, e.symbol, e.event_type, e.exchange_ts, e.received_ts, data))
                    if await cursor.fetchone() is None:
                        continue
                    created += 1
                    # P28: admit closed candles to the SAME historical store used by
                    # walk-forward research. Never admit forming candles or rewrite
                    # historical values after an exchange event is committed.
                    from trader.operations.closed_candles import closed_bar
                    closed = closed_bar(e)
                    if closed is not None:
                        await conn.execute("""INSERT INTO historical_bars
                           (market,symbol,interval,open_ms,close_ms,o,h,l,c,volume,source)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'binance_public_ws')
                           ON CONFLICT (market,symbol,interval,open_ms) DO NOTHING""",
                           (closed.market,closed.symbol,closed.interval,closed.open_ms,
                            closed.close_ms,closed.o,closed.h,closed.l,closed.c,closed.volume))
                        stored = await (await conn.execute("""SELECT close_ms,o,h,l,c,volume
                           FROM historical_bars WHERE market=%s AND symbol=%s AND interval=%s
                           AND open_ms=%s""",(closed.market,closed.symbol,closed.interval,
                                               closed.open_ms))).fetchone()
                        if stored != (closed.close_ms,closed.o,closed.h,closed.l,closed.c,closed.volume):
                            raise ValueError('CLOSED_CANDLE_HISTORY_CONFLICT: quarantine and investigate')
                    await conn.execute("""
                        INSERT INTO market_latest(market,symbol,event_type,event_id,exchange_ts,received_ts,payload)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(market,symbol,event_type) DO UPDATE SET
                          event_id=EXCLUDED.event_id, exchange_ts=EXCLUDED.exchange_ts,
                          received_ts=EXCLUDED.received_ts, payload=EXCLUDED.payload
                        WHERE market_latest.received_ts <= EXCLUDED.received_ts
                    """, (e.market, e.symbol, e.event_type, e.event_id, e.exchange_ts, e.received_ts, data))
                    if e.event_type == "agg_trade":
                        trade_id = int(e.payload["trade_id"])
                        seq = await (await conn.execute("""
                            SELECT last_id FROM aggregate_trade_offsets
                            WHERE market=%s AND symbol=%s FOR UPDATE
                        """, (e.market, e.symbol))).fetchone()
                        if seq and trade_id > seq[0] + 1:
                            await conn.execute("""
                                INSERT INTO market_data_gaps(market,symbol,category,expected_next,observed_next)
                                VALUES (%s,%s,'agg_trade_sequence',%s,%s)
                            """, (e.market, e.symbol, seq[0] + 1, trade_id))
                        if not seq or trade_id > seq[0]:
                            await conn.execute("""
                                INSERT INTO aggregate_trade_offsets(market,symbol,last_id)
                                VALUES (%s,%s,%s) ON CONFLICT(market,symbol)
                                DO UPDATE SET last_id=EXCLUDED.last_id,updated_at=now()
                            """, (e.market, e.symbol, trade_id))
                    await conn.execute("""
                        INSERT INTO outbox_events(event_id,subject,data)
                        VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
                    """, (e.event_id, f"market.{e.market}.{e.event_type}", Jsonb(e.as_dict())))
        return created

    async def status(self, market: str, connected: bool, error: str | None = None,
                     last_event_ts: int | None = None, reconnect: bool = False) -> None:
        assert self.pool
        async with self.pool.connection() as conn:
            await conn.execute("""
                INSERT INTO stream_status(market,connected,last_event_ts,last_connected_at,last_error,reconnects)
                VALUES (%s,%s,%s,CASE WHEN %s THEN now() ELSE NULL END,%s,%s)
                ON CONFLICT(market) DO UPDATE SET connected=EXCLUDED.connected,
                  last_event_ts=COALESCE(EXCLUDED.last_event_ts,stream_status.last_event_ts),
                  last_connected_at=CASE WHEN EXCLUDED.connected THEN now()
                                        ELSE stream_status.last_connected_at END,
                  last_error=EXCLUDED.last_error,
                  reconnects=stream_status.reconnects+EXCLUDED.reconnects,
                  updated_at=now()
            """, (market, connected, last_event_ts, connected, error, 1 if reconnect else 0))

    async def save_instruments(self, market: str, symbols: list[dict[str, Any]]) -> None:
        from psycopg.types.json import Jsonb
        assert self.pool
        async with self.pool.connection() as conn:
            async with conn.transaction():
                for item in symbols:
                    await conn.execute("""
                        INSERT INTO instruments(market,symbol,base_asset,quote_asset,status,filters,raw)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(market,symbol) DO UPDATE SET
                        base_asset=EXCLUDED.base_asset,quote_asset=EXCLUDED.quote_asset,
                        status=EXCLUDED.status,filters=EXCLUDED.filters,raw=EXCLUDED.raw,
                        refreshed_at=now()
                    """, (market, item["symbol"], item["baseAsset"], item["quoteAsset"],
                          item.get("status", "UNKNOWN"), Jsonb(item.get("filters", [])), Jsonb(item)))

    async def latest(self, market: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
        assert self.pool
        query = "SELECT market,symbol,event_type,event_id,exchange_ts,received_ts,payload FROM market_latest WHERE 1=1"
        args: list[str] = []
        if market:
            query += " AND market=%s"
            args.append(market)
        if symbol:
            query += " AND symbol=%s"
            args.append(symbol)
        query += " ORDER BY market,symbol,event_type LIMIT 200"
        async with self.pool.connection() as conn:
            cursor = await conn.execute(query, args)
            return [dict(zip(("market", "symbol", "event_type", "event_id", "exchange_ts", "received_ts", "payload"), row))
                    for row in await cursor.fetchall()]

    async def streams(self) -> list[dict[str, Any]]:
        assert self.pool
        async with self.pool.connection() as conn:
            cursor = await conn.execute("SELECT market,connected,last_event_ts,reconnects,last_error,updated_at FROM stream_status ORDER BY market")
            return [dict(zip(("market", "connected", "last_event_ts", "reconnects", "last_error", "updated_at"), row))
                    for row in await cursor.fetchall()]

    async def instruments(self, market: str | None = None) -> list[dict[str, Any]]:
        assert self.pool
        async with self.pool.connection() as conn:
            if market:
                cur = await conn.execute("SELECT market,symbol,base_asset,quote_asset,status,filters,refreshed_at FROM instruments WHERE market=%s ORDER BY symbol", (market,))
            else:
                cur = await conn.execute("SELECT market,symbol,base_asset,quote_asset,status,filters,refreshed_at FROM instruments ORDER BY market,symbol")
            keys = ("market", "symbol", "base_asset", "quote_asset", "status", "filters", "refreshed_at")
            return [dict(zip(keys, x)) for x in await cur.fetchall()]

    async def user_by_name(self, username: str) -> tuple | None:
        assert self.pool
        async with self.pool.connection() as conn:
            return await (await conn.execute("SELECT id,username,password_hash,role,totp_secret,enabled FROM users WHERE username=%s", (username,))).fetchone()

    async def user_by_id(self, user_id: int) -> tuple | None:
        assert self.pool
        async with self.pool.connection() as conn:
            return await (await conn.execute("SELECT id,role,enabled FROM users WHERE id=%s", (user_id,))).fetchone()

    async def audit(self, actor: str, action: str, outcome: str, metadata: dict | None = None) -> None:
        from psycopg.types.json import Jsonb
        assert self.pool
        async with self.pool.connection() as conn:
            await conn.execute("INSERT INTO audit_logs(actor,action,outcome,metadata) VALUES (%s,%s,%s,%s)",
                               (actor, action, outcome, Jsonb(metadata or {})))

    async def unresolved_gaps(self) -> list[dict[str, Any]]:
        assert self.pool
        async with self.pool.connection() as conn:
            cur = await conn.execute("""
                SELECT market,symbol,category,expected_next,observed_next,detected_at
                FROM market_data_gaps WHERE resolved_at IS NULL
                ORDER BY detected_at DESC LIMIT 100
            """)
            keys = ("market", "symbol", "category", "expected_next", "observed_next", "detected_at")
            return [dict(zip(keys, row)) for row in await cur.fetchall()]

    async def unpublished(self, limit: int = 100) -> list[tuple[int, str, str, dict]]:
        assert self.pool
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT id,event_id,subject,data FROM outbox_events WHERE published_at IS NULL ORDER BY id LIMIT %s", (limit,))
            return list(await cur.fetchall())

    async def published(self, event_id: str) -> None:
        assert self.pool
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE outbox_events SET published_at=now() WHERE event_id=%s AND published_at IS NULL", (event_id,))

    async def publish_failed(self, event_id: str) -> None:
        assert self.pool
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE outbox_events SET retry_count=retry_count+1 WHERE event_id=%s", (event_id,))
