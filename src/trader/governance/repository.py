"""Durable, single-use approval workflow; reviewers must be independent administrators."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from trader.governance.core import ApprovalError, can_consume, can_review, digest
from trader.exchange.repository import Jsonb


class GovernanceRepo:
    def __init__(self, storage):
        self.storage = storage

    async def create(self, kind: str, payload: dict, requester_id: int,
                     expires_minutes: int = 15) -> dict:
        if kind not in ('SANDBOX_ORDER', 'SHADOW_MODEL_PROMOTION'):
            raise ApprovalError('Unsupported authorization action')
        if expires_minutes < 1 or expires_minutes > 60:
            raise ApprovalError('Invalid request TTL')
        request_id = str(uuid4())
        async with self.storage.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute('''INSERT INTO governance_requests
                    (id,kind,requester_id,payload,payload_sha256,expires_at)
                    VALUES (%s,%s,%s,%s,%s,now()+(%s * interval '1 minute'))''',
                    (request_id,kind,requester_id,Jsonb(payload),digest(payload),expires_minutes))
                await conn.execute('''INSERT INTO governance_events
                    (request_id,actor_id,event_type) VALUES (%s,%s,'REQUESTED')''',
                    (request_id,requester_id))
        return {'id':request_id,'kind':kind,'status':'PENDING','expires_minutes':expires_minutes}

    async def list(self, limit=60):
        async with self.storage.pool.connection() as conn:
            rows = await (await conn.execute('''SELECT id,kind,requester_id,reviewer_id,payload,status,
                created_at,expires_at,review_reason FROM governance_requests
                ORDER BY created_at DESC LIMIT %s''',(limit,))).fetchall()
        keys = ('id','kind','requester_id','reviewer_id','payload','status',
                'created_at','expires_at','review_reason')
        return [dict(zip(keys,r)) for r in rows]

    async def review(self, request_id: str, reviewer_id: int,
                     decision: str, reason: str = '') -> dict:
        if decision not in ('APPROVED','REJECTED'):
            raise ApprovalError('Invalid approval decision')
        async with self.storage.pool.connection() as conn:
            async with conn.transaction():
                row=await (await conn.execute('''SELECT requester_id,kind,status,expires_at,payload
                    FROM governance_requests WHERE id=%s FOR UPDATE''',(request_id,))).fetchone()
                if not row:
                    raise ApprovalError('Approval request not found')
                requester,kind,status,expires_at,payload=row
                can_review(requester=int(requester),reviewer=reviewer_id,status=status,
                           expired=expires_at <= datetime.now(timezone.utc))
                if kind == 'SHADOW_MODEL_PROMOTION' and decision == 'APPROVED':
                    model = await (await conn.execute('''SELECT id,status,quality_pass FROM research_models
                            WHERE id=%s FOR UPDATE''',(payload['model_id'],))).fetchone()
                    if not model or model[1]!='CANDIDATE' or not model[2]:
                        raise ApprovalError('Model not eligible for shadow promotion')
                await conn.execute('''UPDATE governance_requests SET status=%s,
                  reviewer_id=%s,review_reason=%s,reviewed_at=now() WHERE id=%s''',
                  (decision,reviewer_id,reason[:500],request_id))
                await conn.execute('''INSERT INTO governance_events
                   (request_id,actor_id,event_type,detail) VALUES (%s,%s,%s,%s)''',
                   (request_id,reviewer_id,decision,Jsonb({'reason':reason[:500]})))
        return {'id':request_id,'status':decision,'reviewer_id':reviewer_id}

    async def promote_shadow(self, request_id: str, actor_id: int) -> str:
        """Promotes to SHADOW ONLY. Cannot modify execution, account or risk policies."""
        async with self.storage.pool.connection() as conn:
            async with conn.transaction():
                row=await (await conn.execute('''SELECT kind,status,expires_at,payload,payload_sha256
                    FROM governance_requests WHERE id=%s FOR UPDATE''',(request_id,))).fetchone()
                if not row or row[0] != 'SHADOW_MODEL_PROMOTION':
                    raise ApprovalError('Shadow model approval required')
                kind,status,expiry,payload,payload_hash=row
                can_consume(status=status,expired=expiry <= datetime.now(timezone.utc),
                            actual=payload,expected_sha256=payload_hash)
                model=await (await conn.execute('''SELECT market,symbol,candle_interval,status,quality_pass
                    FROM research_models WHERE id=%s FOR UPDATE''',
                    (payload['model_id'],))).fetchone()
                if not model or model[3]!='CANDIDATE' or not model[4]:
                    raise ApprovalError('Invalid candidate model state')
                await conn.execute('''UPDATE research_models SET status='RETIRED' WHERE
                    market=%s AND symbol=%s AND candle_interval=%s AND status='SHADOW' ''',model[:3])
                await conn.execute('''UPDATE research_models SET status='SHADOW',promoted_at=now()
                    WHERE id=%s''',(payload['model_id'],))
                await conn.execute('''UPDATE governance_requests SET status='CONSUMED'
                    WHERE id=%s''',(request_id,))
                await conn.execute('''INSERT INTO governance_events
                    (request_id,actor_id,event_type) VALUES (%s,%s,'SHADOW_PROMOTED')''',
                    (request_id,actor_id))
        return str(payload['model_id'])
