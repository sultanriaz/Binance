"""Dedicated production execution process; strictly separate from public API and research."""
from __future__ import annotations
import asyncio
import logging
import os
import psycopg
from trader.config import Settings
from trader.live.settings import LiveSettings
from trader.live.adapter import LiveTransport, LiveAdapter
from trader.live.engine import LiveEngine,LiveExecutionHalt
from trader.live.repository import LiveRepository
from trader.live.automation import maybe_schedule_spot
from trader.exchange.transport import ExchangeError
from trader.storage import Storage
from trader.operations.repository import OperationsRepository

LOG=logging.getLogger(__name__)
LOCK=886602922061

async def main():
    logging.basicConfig(level=logging.INFO)
    config=LiveSettings.load()
    if not config.enabled or not config.confirmed:
        LOG.warning('LIVE TRADING IS DISABLED; no production requests will be transmitted')
        return
    cfg=Settings.load()
    if os.getenv('LIVE_FUTURES_ELIGIBILITY_CONFIRMED','false').lower()!='true' and config.configured('futures-live'):
        raise RuntimeError('Futures account eligibility acknowledgement required')
    db=Storage(cfg.database_url)
    await db.open()
    lock=await psycopg.AsyncConnection.connect(cfg.database_url,autocommit=True)
    transports=[]
    try:
        taken=await (await lock.execute('SELECT pg_try_advisory_lock(%s)',(LOCK,))).fetchone()
        if not taken[0]:raise RuntimeError('Another LIVE EXCHANGE WRITER owns the global lock')
        repo=LiveRepository(db)
        await repo.recover()
        engines=[]
        for code,key,secret in (('spot-live',config.spot_key,config.spot_secret),
                                ('futures-live',config.futures_key,config.futures_secret)):
            if not config.configured(code):
                LOG.warning('%s live credentials not fully bound; account DISABLED',code)
                continue
            t=LiveTransport(code,key,secret)
            transports.append(t)
            await t.synchronize()
            engines.append(LiveEngine(code,LiveAdapter(t),repo,config))
        if not engines:raise RuntimeError('No fingerprint-bound live credentials configured')
        while True:
            for engine in engines:
                try:
                    state=await engine.process_once()
                    await OperationsRepository(db).heartbeat('live-worker',detail={'account':engine.code})
                    if engine.code=='spot-live' and state=='idle':
                        state=await maybe_schedule_spot(repo,config)
                    if state not in ('idle','disarmed','disabled','already_processed','hold'):
                        LOG.info('Live account %s state: %s',engine.code,state)
                except (LiveExecutionHalt,ExchangeError) as exc:
                    LOG.error('Live account %s STOP: %s',engine.code,str(exc))
                    # Reconciliation failures must never turn into repeated order submissions.
                    await repo.set_halt(engine.code)
                except Exception:
                    LOG.exception('Unexpected live worker error; disarming %s',engine.code)
                    await repo.set_halt(engine.code)
            await asyncio.sleep(config.poll_seconds)
    finally:
        for t in transports:await t.close()
        await lock.close()
        await db.close()

if __name__=='__main__':
    asyncio.run(main())
