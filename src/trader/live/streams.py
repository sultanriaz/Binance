"""Production WebSocket User Data Streams for Binance Spot and USD-M Futures.

Authoritative state remains REST reconciliation. Streams provide auxiliary sub-second
reactivity to triggers, fills, and margin calls without waiting for polling intervals.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from urllib.parse import urlencode

import websockets

from trader.live.settings import LIVE_WS_HOSTS
from trader.live.alerts import send_alert

LOG = logging.getLogger(__name__)


def event_key(e: dict) -> str:
    kind = e.get('e', 'unknown')
    order = e.get('i') or e.get('o', {}).get('i') or ''
    trade = e.get('t') or e.get('o', {}).get('t') or ''
    stamp = e.get('T') or e.get('E') or e.get('u') or 0
    return hashlib.sha256(f'{kind}:{order}:{trade}:{stamp}:{json.dumps(e, sort_keys=True)}'.encode()).hexdigest()


async def watch_spot(adapter, repo, on_event=None):
    """Subscribe to Binance Spot WebSocket API via signed userDataStream.subscribe.signature."""
    t = adapter.t
    url = LIVE_WS_HOSTS['spot-live']
    async with websockets.connect(url, ping_interval=20, max_size=2**20) as ws:
        params = {
            'apiKey': t.api_key,
            'timestamp': int(time.time() * 1000) + t.clock_offset,
            'recvWindow': 5000,
        }
        payload = urlencode(sorted(params.items()))
        from hmac import new
        params['signature'] = new(t.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        await ws.send(json.dumps({'id': 'sub-spot-live', 'method': 'userDataStream.subscribe.signature', 'params': params}))
        response = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if response.get('status') != 200 or not response.get('result'):
            raise RuntimeError(f'Spot user data stream subscription rejected: {response}')
        LOG.info("Spot live user data stream subscribed successfully")

        async for message in ws:
            obj = json.loads(message)
            event = obj.get('event', obj)
            if not isinstance(event, dict) or not event.get('e'):
                continue
            event_type = event.get('e')
            LOG.info("Spot live stream event received: %s", event_type)
            if on_event:
                try:
                    on_event(event)
                except Exception as exc:
                    LOG.warning("Error in on_event callback: %s", exc)
            if event_type == 'eventStreamTerminated':
                raise RuntimeError('Spot user stream terminated by server')


async def watch_futures(adapter, repo, on_event=None):
    """Subscribe to Binance USD-M Futures User Data Stream via listenKey with automated keepalive."""
    key = await adapter.start_futures_stream()
    LOG.info("Obtained futures user data listenKey")

    async def heartbeat():
        while True:
            await asyncio.sleep(25 * 60)
            try:
                await adapter.keepalive_futures_stream(key)
                LOG.info("Futures user stream listenKey keepalive renewed")
            except Exception as exc:
                LOG.warning("Futures stream keepalive failed: %s", exc)

    task = asyncio.create_task(heartbeat())
    try:
        url = f"{LIVE_WS_HOSTS['futures-live']}/ws/{key}"
        async with websockets.connect(url, ping_interval=20, max_size=2**20) as ws:
            LOG.info("Futures live user data stream connected")
            async for message in ws:
                event = json.loads(message)
                if not isinstance(event, dict) or not event.get('e'):
                    continue
                event_type = event.get('e')
                LOG.info("Futures live stream event received: %s", event_type)
                if event_type == 'MARGIN_CALL':
                    LOG.critical("EXCHANGE MARGIN CALL RECEIVED: %s", event)
                    asyncio.create_task(send_alert(
                        title="BINANCE FUTURES MARGIN CALL",
                        message=f"Critical margin call event received on futures account! Positions at extreme risk of liquidation.",
                        level="CRITICAL",
                        metadata=event
                    ))
                if on_event:
                    try:
                        on_event(event)
                    except Exception as exc:
                        LOG.warning("Error in on_event callback: %s", exc)
                if event_type == 'listenKeyExpired':
                    raise RuntimeError('Futures user stream listenKey expired')
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def stream_loop(code: str, adapter, repo, on_event=None):
    """Resilient reconnect loop for user data streams."""
    while True:
        try:
            if code == 'spot-live':
                await watch_spot(adapter, repo, on_event)
            else:
                await watch_futures(adapter, repo, on_event)
        except asyncio.CancelledError:
            LOG.info("User data stream for %s stopped", code)
            raise
        except Exception as exc:
            LOG.warning("Live user stream for %s disconnected (%s: %s). Reconnecting in 5s...",
                        code, type(exc).__name__, exc)
        await asyncio.sleep(5)

