"""Single-owner Binance sandbox executor. Never runs against production endpoints."""
from __future__ import annotations

import asyncio
import logging

import psycopg

from trader.config import Settings
from trader.exchange.adapters import SandboxAdapter
from trader.exchange.engine import ReconcileError, SandboxEngine
from trader.exchange.repository import ExchangeRepo
from trader.exchange.settings import ExchangeSettings
from trader.exchange.streams import stream_loop
from trader.exchange.transport import ExchangeError, SandboxTransport
from trader.storage import Storage
from trader.operations.repository import OperationsRepository

LOG = logging.getLogger(__name__)
LOCK_KEY = 467154610328721


async def main():
    cfg = Settings.load()
    s = ExchangeSettings.load()
    logging.basicConfig(level=getattr(logging,cfg.log_level,logging.INFO))
    if not s.active or not s.confirmed:
        LOG.warning('Sandbox execution disabled. No exchange worker started.')
        return
    if not 2 <= s.poll_seconds <= 60:
        raise ValueError('EXCHANGE_POLL_SECONDS must be 2–60')
    storage = Storage(cfg.database_url)
    await storage.open()
    # Advisory-lock connection persists throughout the lifetime of this runner.
    lock_conn = await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True)
    jobs = []
    transports = []
    try:
        exclusive = await (await lock_conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK_KEY,))).fetchone()
        if not exclusive[0]:
            raise RuntimeError('A sandbox exchange writer already owns the global database lock')
        repo = ExchangeRepo(storage)
        await repo.ensure_accounts()
        await repo.recover_crashed_writes()
        engines = []
        for code, key, secret in [('spot-testnet', s.spot_key, s.spot_secret),
                                  ('futures-demo',s.futures_key,s.futures_secret)]:
            if not s.configured(code):
                LOG.warning('%s not configured; no orders or subscriptions for this account', code)
                continue
            transport = SandboxTransport(code,key,secret)
            transports.append(transport)
            adapter = SandboxAdapter(transport)
            await transport.synchronize()
            engines.append(SandboxEngine(code,adapter,repo,s))
            jobs.append(asyncio.create_task(stream_loop(code,adapter,repo),name=f'{code}-user-stream'))
        if not engines:
            raise RuntimeError('No sandbox API credentials configured')
        while True:
            for eng in engines:
                try:
                    action = await eng.process_once()
                    await OperationsRepository(storage).heartbeat('exchange-worker')
                    if action not in ('idle','disarmed'):
                        LOG.info('sandbox %s: %s',eng.code,action)
                except ReconcileError as exc:
                    LOG.warning('Sandbox account %s HALTED: %s', eng.code, str(exc))
                except ExchangeError as exc:
                    await repo.mark_account(eng.code, 'HALTED', error='EXCHANGE_UNAVAILABLE')
                    LOG.error('Sandbox account %s exchange unavailable: %s',eng.code,type(exc).__name__)
                except Exception:
                    await repo.mark_account(eng.code,'HALTED',error='UNEXPECTED_WORKER_FAILURE')
                    LOG.exception('Sandbox worker error on %s: new trades disabled',eng.code)
            await asyncio.sleep(s.poll_seconds)
    finally:
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)
        for transport in transports:
            await transport.close()
        await lock_conn.close()
        await storage.close()


if __name__ == '__main__':
    asyncio.run(main())
