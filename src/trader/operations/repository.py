"""Durable operational monitoring and append-only readiness snapshots."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from trader.exchange.repository import Jsonb
from trader.operations.readiness import evaluate, evidence_digest


class OperationsRepository:
    def __init__(self, db):
        self.db = db

    async def heartbeat(self, service: str, status: str = 'HEALTHY', detail: dict | None = None):
        if service not in {'collector','paper-worker','research-worker','outbox',
                           'operations-worker','exchange-worker','live-worker'}:
            raise ValueError('Unknown worker identity')
        if status not in {'HEALTHY','DEGRADED','HALTED'}:
            raise ValueError('Invalid heartbeat status')
        async with self.db.pool.connection() as conn:
            await conn.execute('''INSERT INTO service_heartbeats(service,status,detail)
                VALUES(%s,%s,%s) ON CONFLICT(service) DO UPDATE SET
                last_seen=now(),status=EXCLUDED.status,detail=EXCLUDED.detail''',
                (service,status,Jsonb(detail or {})))

    async def evidence(self) -> dict:
        now = datetime.now(timezone.utc)
        async with self.db.pool.connection() as conn:
            async def scalar(sql):
                return int((await (await conn.execute(sql)).fetchone())[0])
            streams = await (await conn.execute('''SELECT market,connected,last_event_ts
               FROM stream_status ORDER BY market''')).fetchall()
            beats = await (await conn.execute('''SELECT service,status,last_seen
               FROM service_heartbeats ORDER BY service''')).fetchall()
            models = await (await conn.execute('''SELECT m.id,m.status,COUNT(p.resolved_at) AS resolved_count,
              EXTRACT(EPOCH FROM (MAX(p.resolved_at)-MIN(p.resolved_at)))/86400.0 AS span_days,
              AVG(POWER(p.p_up - CASE WHEN p.resolved_net_return>0 THEN 1.0 ELSE 0.0 END,2))
                 FILTER (WHERE p.resolved_at IS NOT NULL) AS brier,
              EXTRACT(EPOCH FROM now()-MAX(p.created_at)) AS age_seconds
              FROM research_models m LEFT JOIN shadow_predictions p ON p.model_id=m.id
              WHERE m.status='SHADOW' GROUP BY m.id,m.status ORDER BY m.id''')).fetchall()
            # Backups require a recovery-tested operator attestation. A mere file does not prove restoration.
            backup = await (await conn.execute("""SELECT EXTRACT(EPOCH FROM now()-MAX(verified_at))
                  FROM backup_attestations WHERE method='DISPOSABLE_FULL_RESTORE'""")).fetchone()
            return {
                'database_healthy': True,
                'collector_streams': [{'market':m,'connected':c,
                    'age_seconds':None if t is None else max(0,(int(time.time()*1000)-t)/1000)}
                    for m,c,t in streams],
                'worker_heartbeats': [{'service':s,'status':v,
                    'age_seconds':max(0,(now-t).total_seconds())} for s,v,t in beats],
                'unresolved_market_gaps':await scalar('''SELECT COUNT(*) FROM market_data_gaps
                  WHERE resolved_at IS NULL'''),
                'open_exchange_incidents':await scalar('''SELECT COUNT(*) FROM exchange_incidents
                  WHERE acknowledged_at IS NULL'''),
                'unknown_exchange_orders':await scalar('''SELECT COUNT(*) FROM exchange_order_intents
                  WHERE status IN ('UNKNOWN','SUBMITTING','CANCEL_PENDING')'''),
                'open_operations_incidents':await scalar('''SELECT COUNT(*) FROM operations_incidents
                  WHERE resolved_at IS NULL'''),
                'shadow_models':[{'id':str(mid),'status':state,'resolved_count':int(n),
                    'span_days':float(days) if days is not None else None,
                    'brier':float(b) if b is not None else None,
                    'age_seconds':float(age) if age is not None else None}
                    for mid,state,n,days,b,age in models],
                'backup_age_seconds':float(backup[0]) if backup and backup[0] is not None else None,
            }

    async def snapshot(self):
        # Use a session advisory lock to serialize hash linkage across instances.
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock(72903145)')
                prior = await (await conn.execute('''SELECT sha256 FROM operations_readiness
                        ORDER BY id DESC LIMIT 1''')).fetchone()
                # Evidence reads use a separate DB pool connection; serialized writes only.
                evidence = await self.evidence()
                report = evaluate(evidence)
                prev = prior[0] if prior else None
                checksum = evidence_digest(report, prev)
                await conn.execute('''INSERT INTO operations_readiness
                   (verdict,policy_version,evidence,sha256,prior_sha256)
                   VALUES (%s,%s,%s,%s,%s)''',
                   (report['verdict'],report['policy_version'],Jsonb(report),checksum,prev))
                return {'verdict':report['verdict'],'sha256':checksum,'report':report}

    async def recent(self, limit: int = 30):
        async with self.db.pool.connection() as conn:
            rows = await (await conn.execute('''SELECT id,evaluated_at,verdict,sha256,
               prior_sha256,evidence FROM operations_readiness ORDER BY id DESC LIMIT %s''',
               (min(max(limit,1),100),))).fetchall()
        return [dict(id=r[0],evaluated_at=r[1],verdict=r[2],sha256=r[3],
                     previous_sha256=r[4],report=r[5]) for r in rows]

    async def open_incident(self, component: str, code: str, details: dict | None = None):
        async with self.db.pool.connection() as conn:
            await conn.execute('''INSERT INTO operations_incidents(component,code,details)
               VALUES (%s,%s,%s) ON CONFLICT(component,code) WHERE resolved_at IS NULL
               DO NOTHING''',(component,code,Jsonb(details or {})))

    async def incidents(self):
        async with self.db.pool.connection() as conn:
            rows = await (await conn.execute('''SELECT id,component,code,opened_at
                FROM operations_incidents WHERE resolved_at IS NULL
                ORDER BY opened_at DESC LIMIT 100''')).fetchall()
        return [dict(id=r[0],component=r[1],code=r[2],opened_at=r[3]) for r in rows]
