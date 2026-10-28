"""User-data stream observability for BOTH 2026 Binance sandbox products.

REST reconciliation is authoritative. The stream is auxiliary and cannot alone
clear an uncertain order. Spot uses signed WS API subscription, NOT listenKey.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from urllib.parse import urlencode

import websockets

from trader.exchange.settings import HOSTS

LOG = logging.getLogger(__name__)


def event_key(e: dict):
    kind = e.get('e','unknown')
    order = e.get('i') or e.get('o',{}).get('i') or ''
    trade = e.get('t') or e.get('o',{}).get('t') or ''
    stamp = e.get('T') or e.get('E') or e.get('u') or 0
    return hashlib.sha256(f'{kind}:{order}:{trade}:{stamp}:{json.dumps(e,sort_keys=True)}'.encode()).hexdigest()


async def watch_spot(adapter, repo):
    """Subscribe through HMAC signed `userDataStream.subscribe.signature`."""
    t = adapter.t
    url = HOSTS['spot-testnet'][1]
    async with websockets.connect(url, ping_interval=20, max_size=2**20) as ws:
        params = {'apiKey':t.api_key,'timestamp':int(time.time()*1000)+t.clock_offset,'recvWindow':5000}
        # WebSocket signature payload is alphabetically sorted parameter keys.
        payload = urlencode(sorted(params.items()))
        from hmac import new
        params['signature'] = new(t.secret.encode(),payload.encode(),hashlib.sha256).hexdigest()
        await ws.send(json.dumps({'id':'subscribe', 'method':'userDataStream.subscribe.signature','params':params}))
        response = json.loads(await asyncio.wait_for(ws.recv(),timeout=15))
        if response.get('status') != 200 or not response.get('result'):
            raise RuntimeError('Spot user data stream subscription rejected')
        async for message in ws:
            obj = json.loads(message)
            event = obj.get('event', obj)
            if not isinstance(event,dict) or not event.get('e'):
                continue
            await repo.stream_event('spot-testnet',event_key(event),event['e'],event)
            if event.get('e') == 'eventStreamTerminated':
                raise RuntimeError('Spot user stream terminated')


async def watch_futures(adapter, repo):
    key = await adapter.start_futures_stream()
    async def heartbeat():
        while True:
            await asyncio.sleep(25*60)
            await adapter.keepalive_futures_stream(key)
    task = asyncio.create_task(heartbeat())
    try:
        async with websockets.connect(HOSTS['futures-demo'][1] + '/ws/' + key,
                                      ping_interval=20, max_size=2**20) as ws:
            async for message in ws:
                event = json.loads(message)
                if not isinstance(event,dict) or not event.get('e'):
                    continue
                await repo.stream_event('futures-demo',event_key(event),event['e'],event)
                if event.get('e') == 'listenKeyExpired':
                    raise RuntimeError('Futures user stream expired')
    finally:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)


async def stream_loop(code, adapter, repo):
    while True:
        try:
            if code == 'spot-testnet':
                await watch_spot(adapter,repo)
            else:
                await watch_futures(adapter,repo)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning('Sandbox user stream for %s disconnected: %s',code,type(exc).__name__)
        await asyncio.sleep(8)
