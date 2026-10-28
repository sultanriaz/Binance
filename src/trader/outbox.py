"""At-least-once PostgreSQL transactional outbox delivery to NATS JetStream.

Consumers must deduplicate by event_id. No delivery is assumed exactly once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
from contextlib import suppress

import nats
from nats.js.api import StorageType, StreamConfig

from trader.config import Settings
from trader.storage import Storage
from trader.operations.repository import OperationsRepository

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = Settings.load()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
    store = Storage(settings.database_url)
    await store.open()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    nc = None
    try:
        while not stop.is_set():
            try:
                if nc is None or nc.is_closed:
                    nc = await nats.connect(settings.nats_url, connect_timeout=5, max_reconnect_attempts=5)
                    js = nc.jetstream()
                    try:
                        await js.add_stream(StreamConfig(name="MARKET", subjects=["market.>"],
                                                         storage=StorageType.FILE,
                                                         max_age=7 * 24 * 3600))
                    except Exception as exc:
                        # It may already exist, check existence before accepting.
                        await js.stream_info("MARKET")
                js = nc.jetstream()
                records = await store.unpublished(limit=100)
                for _, event_id, subject, payload in records:
                    body = json.dumps(payload, separators=(",", ":"), default=str).encode()
                    try:
                        await js.publish(subject, body, headers={"Nats-Msg-Id": event_id}, timeout=5)
                        await store.published(event_id)
                    except Exception:
                        await store.publish_failed(event_id)
                        raise
                await OperationsRepository(store).heartbeat('outbox')
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.2 if records else 1)
                except asyncio.TimeoutError:
                    pass
            except Exception as exc:
                logger.warning("outbox publish interrupted (retrying): %s", exc)
                if nc:
                    with suppress(Exception):
                        await nc.close()
                nc = None
                # Outbox failure MUST NOT be reported as healthy.
                try:
                    await OperationsRepository(store).heartbeat('outbox','DEGRADED')
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
    finally:
        if nc:
            with suppress(Exception):
                await nc.drain()
        await store.close()


if __name__ == "__main__":
    asyncio.run(run())
