"""PostgreSQL research job queue, immutable model snapshots, shadow observations."""
from __future__ import annotations
from uuid import uuid4

from trader.exchange.repository import Jsonb
from trader.intelligence.training import candidate_quality


class ResearchRepo:
    def __init__(self, storage):
        self.db = storage

    async def request(self, actor, market, symbol, interval, bars, horizon, friction_bps):
        job_id = str(uuid4())
        async with self.db.pool.connection() as conn:
            await conn.execute('''INSERT INTO research_jobs
                (id,created_by,market,symbol,candle_interval,bars,horizon,friction_bps)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                (job_id,int(actor),market,symbol,interval,bars,horizon,friction_bps))
        return job_id

    async def jobs(self):
        async with self.db.pool.connection() as conn:
            rows = await (await conn.execute('''SELECT id,market,symbol,candle_interval,bars,
                 horizon,friction_bps,state,error_code,model_id,created_at,finished_at
                 FROM research_jobs ORDER BY created_at DESC LIMIT 50''')).fetchall()
        keys=('id','market','symbol','interval','bars','horizon','friction_bps',
              'state','error_code','model_id','created_at','finished_at')
        return [dict(zip(keys,r)) for r in rows]

    async def claim(self):
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                row=await (await conn.execute('''SELECT id,market,symbol,candle_interval,bars,
                  horizon,friction_bps FROM research_jobs WHERE state='QUEUED'
                  ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1''')).fetchone()
                if row:
                    await conn.execute('''UPDATE research_jobs SET state='RUNNING',started_at=now()
                       WHERE id=%s''',(row[0],))
                return row

    async def finish(self, job_id, result):
        passed,reasons=candidate_quality(result)
        model_id=str(uuid4())
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute('''INSERT INTO research_models
                  (id,job_id,market,symbol,candle_interval,horizon,train_data_last_ms,
                   data_sha256,artifact,metrics,limitations,quality_pass,quality_reasons)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                  (model_id,job_id,result['market'],result['symbol'],result['interval'],
                   result['horizon'],result['last_open_ms'],result['data_sha256'],
                   Jsonb(result['artifact']),Jsonb(result['metrics']),
                   Jsonb(result['limitations']),passed,Jsonb(reasons)))
                await conn.execute('''UPDATE research_jobs SET state='SUCCEEDED',
                  model_id=%s,finished_at=now() WHERE id=%s''',(model_id,job_id))
        return model_id

    async def failed(self, job_id, code):
        async with self.db.pool.connection() as conn:
            await conn.execute('''UPDATE research_jobs SET state='FAILED',error_code=%s,
             finished_at=now() WHERE id=%s''',(code[:64],job_id))

    async def models(self):
        async with self.db.pool.connection() as conn:
            rows=await (await conn.execute('''SELECT id,job_id,market,symbol,candle_interval,
              horizon,train_data_last_ms,metrics,quality_pass,quality_reasons,status,created_at,
              promoted_at,shadow_samples_count,rolling_brier,realized_win_rate
              FROM research_models ORDER BY created_at DESC LIMIT 50''')).fetchall()
        keys=('id','job_id','market','symbol','interval','horizon','train_data_last_ms',
              'metrics','quality_pass','quality_reasons','status','created_at','promoted_at',
              'shadow_samples_count','rolling_brier','realized_win_rate')
        return [dict(zip(keys,r)) for r in rows]

    async def get(self, id_):
        async with self.db.pool.connection() as conn:
            row=await (await conn.execute('''SELECT id,market,symbol,candle_interval,horizon,
               train_data_last_ms,artifact,status,quality_pass,shadow_samples_count,
               rolling_brier,realized_win_rate,promoted_at,metrics FROM research_models
               WHERE id=%s''',(id_,))).fetchone()
        keys=('id','market','symbol','interval','horizon','train_data_last_ms',
              'artifact','status','quality_pass','shadow_samples_count',
              'rolling_brier','realized_win_rate','promoted_at','metrics')
        return dict(zip(keys,row)) if row else None

    async def shadow(self, id_):
        async with self.db.pool.connection() as conn:
            rows=await (await conn.execute('''SELECT bar_open_ms,p_up,threshold,signal,
               resolved_net_return,correct_label FROM shadow_predictions WHERE model_id=%s
               ORDER BY bar_open_ms DESC LIMIT 100''',(id_,))).fetchall()
        keys=('bar_open_ms','p_up','threshold','signal','resolved_net_return','correct_label')
        return [dict(zip(keys,r)) for r in rows]

    async def create_campaign(self, actor, market, symbol, interval, bars,
                              horizon, friction_bps, min_new_bars):
        id_=str(uuid4())
        async with self.db.pool.connection() as conn:
            await conn.execute('''INSERT INTO research_campaigns
              (id,created_by,market,symbol,candle_interval,bars,horizon,
               friction_bps,min_new_bars)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
              (id_,actor,market,symbol,interval,bars,horizon,friction_bps,min_new_bars))
        return id_

    async def campaigns(self):
        async with self.db.pool.connection() as conn:
            rows=await (await conn.execute('''SELECT id,market,symbol,candle_interval,bars,
              horizon,friction_bps,min_new_bars,enabled,last_submission_open_ms,
              last_job_id FROM research_campaigns ORDER BY created_at DESC''')).fetchall()
        keys=('id','market','symbol','interval','bars','horizon','friction_bps',
              'min_new_bars','enabled','last_submission_open_ms','last_job_id')
        return [dict(zip(keys,row)) for row in rows]

    async def set_campaign(self,id_,enabled):
        async with self.db.pool.connection() as conn:
            row=await (await conn.execute('''UPDATE research_campaigns
             SET enabled=%s WHERE id=%s RETURNING id''',(enabled,id_))).fetchone()
        return bool(row)

    async def schedule_campaigns(self):
        """One auto candidate per campaign only after enough NEW closed candles.

        Atomic compare under row lock; research candidates are NEVER auto promoted.
        """
        from trader.paper.history import INTERVAL_MS
        import time
        submitted=[]
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                rows=await (await conn.execute('''SELECT id,created_by,market,symbol,
                  candle_interval,bars,horizon,friction_bps,min_new_bars,
                  last_submission_open_ms FROM research_campaigns
                  WHERE enabled IS TRUE FOR UPDATE SKIP LOCKED''')).fetchall()
                for id_,actor,market,symbol,interval,bars,horizon,friction,minimum,last in rows:
                    newest=await (await conn.execute('''SELECT max(open_ms),count(*)
                     FROM (SELECT open_ms FROM historical_bars WHERE market=%s
                     AND symbol=%s AND interval=%s AND close_ms < %s
                     ORDER BY open_ms DESC LIMIT %s) q''',
                      (market,symbol,interval,int(time.time()*1000),bars))).fetchone()
                    if not newest or newest[1]<bars:
                        continue
                    newest_ms=int(newest[0])
                    if last is not None and newest_ms-last < minimum*INTERVAL_MS[interval]:
                        continue
                    busy=await (await conn.execute('''SELECT 1 FROM research_jobs
                        WHERE campaign_id=%s AND state IN ('QUEUED','RUNNING') LIMIT 1''',
                        (id_,))).fetchone()
                    if busy:
                        continue
                    job_id=str(uuid4())
                    await conn.execute('''INSERT INTO research_jobs
                     (id,created_by,market,symbol,candle_interval,bars,horizon,
                      friction_bps,campaign_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                     (job_id,actor,market,symbol,interval,bars,horizon,friction,id_))
                    await conn.execute('''UPDATE research_campaigns
                      SET last_submission_open_ms=%s,last_job_id=%s WHERE id=%s''',
                      (newest_ms,job_id,id_))
                    submitted.append(job_id)
        return submitted

    async def retire_shadow(self,id_):
        async with self.db.pool.connection() as conn:
            row=await (await conn.execute('''UPDATE research_models
             SET status='RETIRED' WHERE id=%s AND status='SHADOW'
             RETURNING id''',(id_,))).fetchone()
        return bool(row)

    async def update_benchmark(self, model_id: str, count: int, rolling_brier: float, realized_win_rate: float) -> None:
        async with self.db.pool.connection() as conn:
            await conn.execute('''UPDATE research_models
                SET shadow_samples_count=%s, rolling_brier=%s, realized_win_rate=%s
                WHERE id=%s''',
                (count, rolling_brier, realized_win_rate, model_id))

    async def promote_to_canary(self, model_id: str, count: int, rolling_brier: float,
                                realized_win_rate: float, metadata: dict | None = None) -> bool:
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                row = await (await conn.execute('''SELECT market, symbol, candle_interval, status
                    FROM research_models WHERE id=%s FOR UPDATE''', (model_id,))).fetchone()
                if not row or row[3] != 'SHADOW':
                    return False
                market, symbol, interval, _ = row
                # Atomically retire any existing CANARY_ACTIVE model for this market/symbol/interval
                await conn.execute('''UPDATE research_models SET status='RETIRED'
                    WHERE market=%s AND symbol=%s AND candle_interval=%s AND status='CANARY_ACTIVE' ''',
                    (market, symbol, interval))
                # Promote candidate to CANARY_ACTIVE
                await conn.execute('''UPDATE research_models
                    SET status='CANARY_ACTIVE', promoted_at=now(),
                        shadow_samples_count=%s, rolling_brier=%s, realized_win_rate=%s
                    WHERE id=%s''',
                    (count, rolling_brier, realized_win_rate, model_id))
                audit_meta = {
                    'model_id': model_id, 'market': market, 'symbol': symbol,
                    'interval': interval, 'shadow_samples_count': count,
                    'rolling_brier': rolling_brier, 'realized_win_rate': realized_win_rate,
                    **(metadata or {})
                }
                await self.db.audit('system:lifecycle', 'MODEL_AUTO_PROMOTED_CANARY', 'success', audit_meta)
                return True

    async def auto_retire_model(self, model_id: str, reason: str, count: int,
                                rolling_brier: float, realized_win_rate: float,
                                metadata: dict | None = None) -> bool:
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                row = await (await conn.execute('''SELECT market, symbol, candle_interval, status
                    FROM research_models WHERE id=%s FOR UPDATE''', (model_id,))).fetchone()
                if not row or row[3] in ('RETIRED',):
                    return False
                market, symbol, interval, prev_status = row
                await conn.execute('''UPDATE research_models
                    SET status='RETIRED', shadow_samples_count=%s, rolling_brier=%s, realized_win_rate=%s
                    WHERE id=%s''',
                    (count, rolling_brier, realized_win_rate, model_id))
                audit_meta = {
                    'model_id': model_id, 'market': market, 'symbol': symbol,
                    'interval': interval, 'previous_status': prev_status,
                    'reason': reason, 'shadow_samples_count': count,
                    'rolling_brier': rolling_brier, 'realized_win_rate': realized_win_rate,
                    **(metadata or {})
                }
                await self.db.audit('system:lifecycle', 'MODEL_AUTO_RETIRED_PERFORMANCE', 'retired', audit_meta)
                return True

    async def demote_model(self, model_id: str, actor: str = 'admin') -> bool:
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                row = await (await conn.execute('''SELECT market, symbol, candle_interval, status
                    FROM research_models WHERE id=%s FOR UPDATE''', (model_id,))).fetchone()
                if not row or row[3] == 'RETIRED':
                    return False
                market, symbol, interval, prev_status = row
                await conn.execute('''UPDATE research_models SET status='RETIRED' WHERE id=%s''', (model_id,))
                audit_meta = {
                    'model_id': model_id, 'market': market, 'symbol': symbol,
                    'interval': interval, 'previous_status': prev_status, 'demoted_by': actor
                }
                await self.db.audit(str(actor), 'research.model.demote', 'accepted', audit_meta)
                return True

    async def get_active_canary(self, market: str | None = None, symbol: str | None = None) -> dict | None:
        query = '''SELECT id,market,symbol,candle_interval,horizon,train_data_last_ms,
                   artifact,metrics,quality_pass,status,created_at,promoted_at,
                   shadow_samples_count,rolling_brier,realized_win_rate
                   FROM research_models WHERE status='CANARY_ACTIVE' '''
        params = []
        if market:
            query += ' AND market=%s'
            params.append(market)
        if symbol:
            query += ' AND symbol=%s'
            params.append(symbol)
        query += ' ORDER BY promoted_at DESC LIMIT 1'
        async with self.db.pool.connection() as conn:
            row = await (await conn.execute(query, tuple(params))).fetchone()
        if not row:
            return None
        keys = ('id','market','symbol','interval','horizon','train_data_last_ms',
                'artifact','metrics','quality_pass','status','created_at','promoted_at',
                'shadow_samples_count','rolling_brier','realized_win_rate')
        d = dict(zip(keys, row))
        wr = d.get('realized_win_rate')
        brier = d.get('rolling_brier')
        if wr is not None and wr >= 0.55 and (brier is None or brier <= 0.22):
            d['win_rate_health'] = 'HEALTHY'
        elif (wr is not None and wr < 0.50) or (brier is not None and brier > 0.25):
            d['win_rate_health'] = 'DEGRADING'
        else:
            d['win_rate_health'] = 'WATCH'
        return d

    async def mark_model_decaying(self, model_id: str, reasons: list[str]) -> bool:
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                row = await (await conn.execute(
                    "SELECT status, market, symbol, candle_interval FROM research_models WHERE id=%s FOR UPDATE",
                    (model_id,)
                )).fetchone()
                if not row or row[0] == 'RETIRED':
                    return False
                status, market, symbol, interval = row
                await conn.execute(
                    "UPDATE research_models SET status='DECAYING' WHERE id=%s",
                    (model_id,)
                )
                audit_meta = {
                    'model_id': model_id, 'market': market, 'symbol': symbol,
                    'interval': interval, 'reasons': reasons
                }
                await self.db.audit('system:drift_watchdog', 'MODEL_MARKED_DECAYING', 'decaying', audit_meta)
                return True

    async def retire_canary_drift(self, model_id: str, reasons: list[str]) -> bool:
        from psycopg.types.json import Jsonb
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                row = await (await conn.execute(
                    "SELECT status, market, symbol, candle_interval FROM research_models WHERE id=%s FOR UPDATE",
                    (model_id,)
                )).fetchone()
                if not row or row[0] == 'RETIRED':
                    return False
                status, market, symbol, interval = row
                await conn.execute(
                    "UPDATE research_models SET status='RETIRED' WHERE id=%s",
                    (model_id,)
                )
                detail = {
                    'model_id': model_id, 'market': market, 'symbol': symbol,
                    'interval': interval, 'reasons': reasons, 'action': 'RETIRED_TO_CASH'
                }
                try:
                    await conn.execute(
                        "INSERT INTO live_incidents(account_code, category, detail) VALUES (%s, %s, %s)",
                        ('spot-live', 'CONCEPT_DRIFT_INTERVENTION', Jsonb(detail))
                    )
                except Exception:
                    pass
                audit_meta = {
                    'model_id': model_id, 'market': market, 'symbol': symbol,
                    'interval': interval, 'reasons': reasons
                }
                await self.db.audit('system:drift_watchdog', 'CONCEPT_DRIFT_INTERVENTION', 'retired', audit_meta)
                return True

    async def trigger_retraining_job(self, market: str, symbol: str, interval: str,
                                     bars: int = 500, horizon: int = 1, friction_bps: float = 15.0,
                                     actor: int = 1) -> str | None:
        async with self.db.pool.connection() as conn:
            busy = await (await conn.execute('''SELECT id FROM research_jobs
                WHERE market=%s AND symbol=%s AND candle_interval=%s
                AND state IN ('QUEUED', 'RUNNING') LIMIT 1''',
                (market, symbol, interval))).fetchone()
            if busy:
                return busy[0]
        return await self.request(actor, market, symbol, interval, bars, horizon, friction_bps)
