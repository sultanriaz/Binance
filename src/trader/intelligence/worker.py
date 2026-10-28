"""Sandbox-isolated research worker: bounded training and shadow observations.

Has zero Binance credentials; its only write access is research data. Does not
submit orders, change paper strategies, or bypass governance.
"""
from __future__ import annotations
import asyncio
import logging
import time
from trader.config import Settings
from trader.intelligence.features import make_features
from trader.intelligence.model import LinearProbabilityModel
from trader.intelligence.repository import ResearchRepo
from trader.intelligence.training import train_candidate
from trader.paper.repository import PaperRepository
from trader.storage import Storage
from trader.operations.repository import OperationsRepository

LOG=logging.getLogger('trader.intelligence.worker')


async def run_job(repo, paper, job):
    jid,market,symbol,interval,count,horizon,friction=job
    try:
        bars=await paper.bars(market,symbol,interval,count)
        if len(bars) != count:
            raise ValueError('MISSING_HISTORY')
        if bars[-1].close_ms >= int(time.time()*1000):
            raise ValueError('UNCLOSED_HISTORY')
        # Training executes off event loop, no exchanges.
        result=await asyncio.to_thread(train_candidate,bars,horizon=horizon,
                                       friction_bps=float(friction))
        await repo.finish(jid,result)
    except (ValueError,ImportError,RuntimeError) as exc:
        LOG.warning('Research job %s rejected: %s',jid,type(exc).__name__)
        await repo.failed(jid,type(exc).__name__.upper())
    except Exception:
        LOG.exception('Research job %s failed unexpectedly',jid)
        await repo.failed(jid,'UNEXPECTED_RESEARCH_FAILURE')


async def observe_shadow(repo, paper):
    async with repo.db.pool.connection() as conn:
        rows=await (await conn.execute('''SELECT id,market,symbol,candle_interval,
          horizon,train_data_last_ms,artifact FROM research_models
          WHERE status='SHADOW' ORDER BY promoted_at DESC LIMIT 30''')).fetchall()
    for model_id,market,symbol,interval,horizon,last_train,artifact in rows:
        model=LinearProbabilityModel.from_artifact(artifact)
        # Enough historical context to calculate every newly arrived closed bar.
        bars=await paper.bars(market,symbol,interval,5000)
        if not bars:
            continue
        usable=[b for b in bars if b.close_ms < int(time.time()*1000)]
        if len(usable)<25:
            continue
        try:
            observations=make_features(usable)
        except ValueError:
            LOG.warning('Shadow input has missing or malformed candles: %s %s',market,symbol)
            continue
        latest_new=[r for r in observations if r.open_ms > last_train][-100:]
        bar_by_ms={b.open_ms:i for i,b in enumerate(usable)}
        async with repo.db.pool.connection() as conn:
            async with conn.transaction():
                for r in latest_new:
                    p=model.probability(r)
                    await conn.execute('''INSERT INTO shadow_predictions
                      (model_id,bar_open_ms,p_up,threshold,signal)
                      VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                      (model_id,r.open_ms,p,model.threshold,
                       'LONG' if p>=model.threshold else 'FLAT'))
                pending=await (await conn.execute('''SELECT bar_open_ms,p_up,signal
                  FROM shadow_predictions WHERE model_id=%s AND resolved_at IS NULL
                  ORDER BY bar_open_ms LIMIT 200''',(model_id,))).fetchall()
                for open_ms,p,signal in pending:
                    idx=bar_by_ms.get(open_ms)
                    if idx is None or idx + horizon >= len(usable):
                        continue
                    enter=float(usable[idx+1].o)
                    exit_price=float(usable[idx+horizon].c)
                    # Conservative fixed 15bps proxy, not a claim of live fill cost.
                    net=(exit_price/enter-1)-0.0015
                    await conn.execute('''UPDATE shadow_predictions SET
                        entry_open_ms=%s,resolved_net_return=%s,correct_label=%s,
                        resolved_at=now() WHERE model_id=%s AND bar_open_ms=%s
                        AND resolved_at IS NULL''',
                        (usable[idx+1].open_ms,net,bool((net>0)==(p>=.5)),model_id,open_ms))


async def main():
    logging.basicConfig(level=logging.INFO)
    db=Storage(Settings.load().database_url)
    await db.open()
    repo=ResearchRepo(db)
    paper=PaperRepository(db)
    try:
        async with db.pool.connection() as conn:
            # No automatic retry of interrupted training: operator must submit a new job.
            await conn.execute('''UPDATE research_jobs SET state='FAILED',
              error_code='WORKER_INTERRUPTED',finished_at=now() WHERE state='RUNNING' ''')
        while True:
            await repo.schedule_campaigns()
            job=await repo.claim()
            if job:
                await run_job(repo,paper,job)
                await OperationsRepository(db).heartbeat('research-worker')
            else:
                try:
                    await observe_shadow(repo,paper)
                    await OperationsRepository(db).heartbeat('research-worker')
                except Exception:
                    LOG.exception('Shadow observation failed; continuing without trading')
                await asyncio.sleep(15)
    finally:
        await db.close()


if __name__=='__main__':
    asyncio.run(main())
