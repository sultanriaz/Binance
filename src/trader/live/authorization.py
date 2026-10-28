"""Single-use independent approvals with cryptographic payload integrity."""
from __future__ import annotations
from datetime import datetime,timezone
from uuid import uuid4
from trader.governance.core import ApprovalError,can_consume,can_review,digest

KINDS={'SPOT_ARM','FUTURES_ARM','SPOT_AUTOMATION','SPOT_ORDER','FUTURES_ORDER'}

class LiveAuthorization:
    def __init__(self,db): self.db=db

    async def create(self, kind, payload, requester):
        from psycopg.types.json import Jsonb
        if kind not in KINDS: raise ApprovalError('Unsupported live approval')
        id_=str(uuid4())
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute('''INSERT INTO live_authorizations(id,kind,requester,payload,payload_sha,expires_at)
                  VALUES(%s,%s,%s,%s,%s,now()+interval '10 minutes')''',
                  (id_,kind,int(requester),Jsonb(payload),digest(payload)))
                await conn.execute('''INSERT INTO live_authorization_events(authorization_id,actor,event)
                  VALUES(%s,%s,'REQUESTED')''',(id_,int(requester)))
        return {'id':id_,'status':'PENDING','kind':kind,'expires_in_seconds':600}

    async def list(self):
        async with self.db.pool.connection() as conn:
            cur=await conn.execute('''SELECT id,kind,requester,reviewer,payload,status,created_at,
              expires_at FROM live_authorizations ORDER BY created_at DESC LIMIT 50''')
            keys=('id','kind','requester','reviewer','payload','status','created_at','expires_at')
            return [dict(zip(keys,row)) for row in await cur.fetchall()]

    async def review(self, id_, reviewer, decision, reason):
        from psycopg.types.json import Jsonb
        if decision not in ('APPROVED','REJECTED'):
            raise ApprovalError('Invalid review decision')
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                row=await (await conn.execute('''SELECT requester,status,expires_at,payload,payload_sha
                  FROM live_authorizations WHERE id=%s FOR UPDATE''',(id_,))).fetchone()
                if not row: raise ApprovalError('Approval not found')
                requester,status,exp,payload,sha=row
                can_review(requester=requester,reviewer=reviewer,status=status,
                           expired=exp<=datetime.now(timezone.utc))
                if digest(payload)!=sha: raise ApprovalError('Approval payload mutated')
                await conn.execute('''UPDATE live_authorizations SET reviewer=%s,status=%s,
                    reason=%s,reviewed_at=now() WHERE id=%s''',(reviewer,decision,reason[:500],id_))
                await conn.execute('''INSERT INTO live_authorization_events(authorization_id,actor,event,detail)
                    VALUES(%s,%s,%s,%s)''',(id_,reviewer,decision,Jsonb({'reason':reason[:500]})))
        return {'id':id_,'status':decision}

async def consume_authorization(conn,id_,kind,actor,payload):
    """Must be called inside the SAME transaction as the authorized state change."""
    row=await (await conn.execute('''SELECT kind,requester,status,expires_at,payload,payload_sha
          FROM live_authorizations WHERE id=%s FOR UPDATE''',(id_,))).fetchone()
    if not row or row[0]!=kind or row[1]!=actor:
        raise ApprovalError('Correct requesting operator and approval kind required')
    _,_,status,exp,actual,sha=row
    can_consume(status=status,expired=exp<=datetime.now(timezone.utc),actual=actual,expected_sha256=sha)
    if digest(payload)!=sha: raise ApprovalError('Approved action payload changed')
    await conn.execute('''UPDATE live_authorizations SET status='CONSUMED',consumed_at=now() WHERE id=%s''',(id_,))
    await conn.execute('''INSERT INTO live_authorization_events(authorization_id,actor,event)
                           VALUES(%s,%s,'CONSUMED')''',(id_,actor))
