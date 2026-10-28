"""Independent read-only Binance market-data ingestion worker."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import signal
import time
from contextlib import suppress

import redis.asyncio as redis
import websockets
from prometheus_client import Counter, Gauge, start_http_server

from trader.binance_rest import PublicBinanceREST
from trader.config import Settings
from trader.market import MarketEvent, UnsupportedEvent, combined_url, decode
from trader.storage import Storage
from trader.operations.repository import OperationsRepository

logger = logging.getLogger(__name__)
RECEIVED = Counter("trader_market_events_received_total", "Normalized events", ["market", "event_type"])
REJECTED = Counter("trader_market_events_rejected_total", "Bad/unsupported events", ["market"])
RECONNECTS = Counter("trader_market_reconnects_total", "Connection restarts", ["market"])
WRITTEN = Counter("trader_market_events_written_total", "New durable events", ["market"])
CONNECTED = Gauge("trader_market_stream_connected", "WebSocket connected", ["market"])
QUEUE_DEPTH = Gauge("trader_market_queue_depth", "Unpersisted events")


async def writer(queue: asyncio.Queue[MarketEvent], store: Storage, stop: asyncio.Event,
                 cache: redis.Redis | None = None) -> None:
    """Batch transactional writes; WS reader cannot silently shed events on overflow."""
    while not stop.is_set() or not queue.empty():
        batch: list[MarketEvent] = []
        try:
            batch.append(await asyncio.wait_for(queue.get(), timeout=0.5))
        except asyncio.TimeoutError:
            continue
        while len(batch) < 100:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        QUEUE_DEPTH.set(queue.qsize())
        try:
            count = await store.write_events(batch)
            await OperationsRepository(store).heartbeat('collector')
            for market in {e.market for e in batch}:
                WRITTEN.labels(market=market).inc(sum(1 for e in batch if e.market == market) if count == len(batch) else 0)
            if cache is not None:
                try:
                    pipe = cache.pipeline(transaction=False)
                    for e in batch:
                        pipe.setex(f"latest:{e.market}:{e.symbol}:{e.event_type}", 60,
                                   json.dumps(e.as_dict(), separators=(",", ":")))
                    await pipe.execute()
                except Exception as exc:
                    logger.warning("Redis cache unavailable, PostgreSQL still authoritative: %s", exc)
        except Exception as exc:
            logger.exception("Durable market-write failure, halting ingestion to avoid silent loss: %s", exc)
            stop.set()
            raise
        finally:
            for _ in batch:
                queue.task_done()


async def stream_market(market: str, settings: Settings, store: Storage,
                        queue: asyncio.Queue[MarketEvent], stop: asyncio.Event) -> None:
    base = settings.spot_wss if market == "spot" else settings.futures_wss
    url = combined_url(base, market, settings.symbols)
    failures = 0
    last_status_write = 0
    while not stop.is_set():
        try:
            async with websockets.connect(url, open_timeout=15, ping_interval=20,
                                          ping_timeout=15, max_size=2**20,
                                          close_timeout=5) as ws:
                failures = 0
                CONNECTED.labels(market=market).set(1)
                await store.status(market, connected=True)
                logger.info("connected to %s public market streams", market)
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=45)
                    except asyncio.TimeoutError as exc:
                        raise RuntimeError("stale market stream (45s inactivity)") from exc
                    try:
                        event = decode(market, msg)
                        if event.symbol not in settings.symbols:
                            raise UnsupportedEvent("unconfigured symbol")
                        # A full queue means the reader cannot keep pace. Close/restart
                        # rather than pretending there were no missing observations.
                        queue.put_nowait(event)
                        RECEIVED.labels(market=market, event_type=event.event_type).inc()
                        QUEUE_DEPTH.set(queue.qsize())
                        if event.received_ts - last_status_write >= 10000:
                            await store.status(market, connected=True, last_event_ts=event.received_ts)
                            last_status_write = event.received_ts
                    except (UnsupportedEvent, ValueError, KeyError, TypeError, json.JSONDecodeError):
                        REJECTED.labels(market=market).inc()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            CONNECTED.labels(market=market).set(0)
            RECONNECTS.labels(market=market).inc()
            failures += 1
            logger.warning("stream %s interrupted: %s", market, exc)
            with suppress(Exception):
                await store.status(market, connected=False, error=str(exc)[:500], reconnect=True)
            delay = min(2 ** min(failures, 6), 60) + random.random()
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
        finally:
            CONNECTED.labels(market=market).set(0)
    with suppress(Exception):
        await store.status(market, connected=False, error="shutdown")


async def refresh_metadata(rest: PublicBinanceREST, store: Storage,
                           settings: Settings, stop: asyncio.Event) -> None:
    while not stop.is_set():
        for market in settings.markets:
            try:
                remote_ts = await rest.server_time(market)
                skew = abs(int(time.time() * 1000) - remote_ts)
                if skew > 5000:
                    logger.warning("host clock skew %sms on %s", skew, market)
                symbols = await rest.exchange_info(market, settings.symbols)
                await store.save_instruments(market, symbols)
                logger.info("%s instrument metadata refreshed (%d symbols)", market, len(symbols))
            except Exception as exc:
                logger.warning("metadata refresh failed for %s: %s", market, exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=6 * 3600)
        except asyncio.TimeoutError:
            continue


async def run() -> None:
    settings = Settings.load()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    start_http_server(8001, addr="0.0.0.0")
    store = Storage(settings.database_url)
    await store.open()
    cache: redis.Redis | None = redis.from_url(settings.redis_url, socket_connect_timeout=2,
                                             socket_timeout=2)
    rest = PublicBinanceREST(settings.spot_rest, settings.futures_rest)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=5000)
    tasks = [asyncio.create_task(writer(queue, store, stop, cache)),
             asyncio.create_task(refresh_metadata(rest, store, settings, stop))]
    tasks += [asyncio.create_task(stream_market(m, settings, store, queue, stop)) for m in settings.markets]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            if task.exception():
                raise task.exception()
        if not stop.is_set():
            await stop.wait()
    finally:
        stop.set()
        for t in tasks[1:]:
            t.cancel()
        await asyncio.gather(*tasks[1:], return_exceptions=True)
        # Queue flush is best effort, but must never report success if DB writes failed.
        try:
            await asyncio.wait_for(queue.join(), 15)
        except asyncio.TimeoutError:
            logger.error("shutdown flush exceeded deadline; replay/reconciliation required")
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        await rest.close()
        if cache:
            await cache.aclose()
        await store.close()


if __name__ == "__main__":
    asyncio.run(run())
