"""Durable testnet order intent, exchange snapshot and append-only observation store."""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

def Jsonb(value):
    # Late import permits pure risk/engine tests without a PostgreSQL driver.
    from psycopg.types.json import Jsonb as PsycopgJsonb
    return PsycopgJsonb(value)

TERMINAL = frozenset(('FILLED','CANCELED','EXPIRED','REJECTED','FAILED_POLICY'))
ACTIVE = frozenset(('ACKNOWLEDGED','NEW','PARTIALLY_FILLED','CANCEL_PENDING'))
ALL = TERMINAL | ACTIVE | {'QUEUED','SUBMITTING','UNKNOWN'}


def digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Conflict(Exception):
    pass


class ExchangeRepo:
    def __init__(self, storage):
        self.storage = storage

    @property
    def pool(self):
        assert self.storage.pool, 'Database pool unavailable'
        return self.storage.pool

    async def ensure_accounts(self):
        async with self.pool.connection() as conn:
            for code, market, env in [('spot-testnet','spot','SPOT_TESTNET'),
                                      ('futures-demo','futures','FUTURES_DEMO')]:
                await conn.execute('''INSERT INTO exchange_accounts(code,market,environment)
                                      VALUES (%s,%s,%s) ON CONFLICT DO NOTHING''', (code,market,env))

    async def accounts(self):
        async with self.pool.connection() as conn:
            cur = await conn.execute('''SELECT code,market,environment,state,operator_enabled,last_reconciled_at,
                last_stream_at,last_error,updated_at FROM exchange_accounts ORDER BY code''')
            return [dict(zip(('code','market','environment','state','operator_enabled','last_reconciled_at',
                              'last_stream_at','last_error','updated_at'), row)) for row in await cur.fetchall()]

    async def get_account(self, code):
        async with self.pool.connection() as conn:
            row = await (await conn.execute('SELECT code,state,operator_enabled,snapshot,last_reconciled_at FROM exchange_accounts WHERE code=%s',(code,))).fetchone()
            return dict(zip(('code','state','operator_enabled','snapshot','last_reconciled_at'),row)) if row else None

    async def enqueue(self, account, client_id, payload, actor, approval_id):
        if not approval_id:
            from trader.governance.core import ApprovalError
            raise ApprovalError('Sandbox order requires a valid approval ID')
        h = digest(payload)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                if approval_id is not None:
                    from datetime import datetime, timezone
                    from trader.governance.core import ApprovalError, can_consume
                    approval = await (await conn.execute('''SELECT kind,status,expires_at,payload,payload_sha256
                        FROM governance_requests WHERE id=%s FOR UPDATE''' ,(approval_id,))).fetchone()
                    if not approval or approval[0] != 'SANDBOX_ORDER':
                        raise ApprovalError('Missing sandbox-order approval')
                    # Authorization covers account, client id and complete order payload.
                    full = dict(payload,account=account,client_order_id=client_id)
                    can_consume(status=approval[1],expired=approval[2]<=datetime.now(timezone.utc),
                                actual=full,expected_sha256=approval[4])
                    if approval[3] != full:
                        raise ApprovalError('Approval order mismatch')
                result = await self._enqueue_with_conn(conn, account, client_id, payload, actor)
                if approval_id is not None:
                    await conn.execute('''UPDATE governance_requests SET status='CONSUMED'
                        WHERE id=%s''',(approval_id,))
                    await conn.execute('''INSERT INTO governance_events
                        (request_id,actor_id,event_type) VALUES (%s,%s,'SANDBOX_QUEUED')''',
                        (approval_id,int(actor)))
                return result

    async def _enqueue_with_conn(self, conn, account, client_id, payload, actor):
        h = digest(payload)
        if True:
            cur = await conn.execute('''INSERT INTO exchange_order_intents(
               id,account_code,client_order_id,payload,payload_sha256,created_by)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT(account_code,client_order_id) DO NOTHING RETURNING id,status''',
               (str(uuid4()),account,client_id,Jsonb(payload),h,str(actor)))
            found = await cur.fetchone()
            if found:
                await conn.execute('''INSERT INTO exchange_order_events(intent_id,event_kind,payload)
                                      VALUES (%s,'QUEUED',%s)''',(found[0],Jsonb({'actor':str(actor)})))
                return {'id':str(found[0]),'status':found[1],'idempotent':False}
            old = await (await conn.execute('''SELECT id,status,payload_sha256 FROM exchange_order_intents
                                                 WHERE account_code=%s AND client_order_id=%s''',
                                          (account,client_id))).fetchone()
            if old[2] != h:
                raise Conflict('Client ID reused for a different order payload')
            return {'id':str(old[0]),'status':old[1],'idempotent':True}

    async def intents(self, account, limit=50):
        async with self.pool.connection() as conn:
            cur = await conn.execute('''SELECT id,client_order_id,payload,status,exchange_order_id,
              cancel_requested,error_code,error_message,created_at,updated_at
              FROM exchange_order_intents WHERE account_code=%s
              ORDER BY created_at DESC LIMIT %s''',(account,limit))
            columns=('id','client_order_id','payload','status','exchange_order_id',
                     'cancel_requested','error_code','error_message','created_at','updated_at')
            return [dict(zip(columns,row)) for row in await cur.fetchall()]

    async def pending(self, account, statuses):
        async with self.pool.connection() as conn:
            cur = await conn.execute('''SELECT id,client_order_id,payload,status,cancel_requested
              FROM exchange_order_intents WHERE account_code=%s AND status=ANY(%s)
              ORDER BY created_at LIMIT 100''', (account,list(statuses)))
            return [dict(zip(('id','client_order_id','payload','status','cancel_requested'),r)) for r in await cur.fetchall()]

    async def set_status(self, id_, status, response=None, *, err=None, order_id=None):
        if status not in ALL:
            raise ValueError('Invalid order status')
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute('''UPDATE exchange_order_intents
                  SET status=%s,exchange_payload=COALESCE(%s,exchange_payload),
                  exchange_order_id=COALESCE(%s,exchange_order_id),
                  error_message=%s,updated_at=now() WHERE id=%s''',
                  (status,Jsonb(response) if response is not None else None,order_id,err,str(id_)))
                await conn.execute('''INSERT INTO exchange_order_events(intent_id,event_kind,payload)
                   VALUES(%s,%s,%s)''',(str(id_),status,Jsonb(response or ({'error':err} if err else {}))))

    async def request_cancel(self, account, id_):
        async with self.pool.connection() as conn:
            cur = await conn.execute('''UPDATE exchange_order_intents SET cancel_requested=TRUE,
              updated_at=now() WHERE account_code=%s AND id=%s AND status = ANY(%s)
              RETURNING status''',(account,str(id_),list(ACTIVE)))
            return await cur.fetchone() is not None

    async def claim_queued(self, account, intent_id):
        # Claim ONLY the exact intent already checked against a fresh exchange snapshot.
        # Never validate A then accidentally submit B when queued timestamps tie.
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute('''SELECT id,client_order_id,payload,status FROM exchange_order_intents
                  WHERE account_code=%s AND id=%s AND status='QUEUED'
                    AND EXISTS (SELECT 1 FROM exchange_accounts a
                      WHERE a.code=exchange_order_intents.account_code
                        AND a.state='READY' AND a.operator_enabled=TRUE
                        AND NOT EXISTS (SELECT 1 FROM exchange_incidents i
                          WHERE i.account_code=a.code AND i.acknowledged_at IS NULL))
                  FOR UPDATE SKIP LOCKED''',(account,str(intent_id)))
                r = await cur.fetchone()
                if not r:
                    return None
                await conn.execute('''UPDATE exchange_order_intents SET status='SUBMITTING',
                  updated_at=now() WHERE id=%s''',(r[0],))
                await conn.execute('''INSERT INTO exchange_order_events(intent_id,event_kind)
                                      VALUES (%s,'SUBMITTING')''',(r[0],))
                return dict(zip(('id','client_order_id','payload','status'),r))

    async def recover_crashed_writes(self):
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute('''UPDATE exchange_order_intents SET status='UNKNOWN',updated_at=now()
                                      WHERE status IN ('SUBMITTING','CANCEL_PENDING')''')
                await conn.execute('''UPDATE exchange_accounts SET operator_enabled=FALSE,
                                      state='HALTED',last_error='WORKER_RESTART_REARM_REQUIRED' ''')

    async def mark_account(self, code, state, *, error=None, snapshot=None):
        if state not in ('READY','HALTED','INCIDENT','RECONCILING'):
            raise ValueError('Bad exchange state')
        async with self.pool.connection() as conn:
            await conn.execute('''UPDATE exchange_accounts SET state=%s,last_error=%s,
              snapshot=COALESCE(%s,snapshot),
              last_reconciled_at=CASE WHEN %s='READY' THEN now() ELSE last_reconciled_at END,
              updated_at=now() WHERE code=%s''',
              (state,error,Jsonb(snapshot) if snapshot is not None else None,state,code))

    async def incident(self, code, category, *, detail=None):
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute('''INSERT INTO exchange_incidents(account_code,severity,category,detail)
                                       VALUES (%s,'CRITICAL',%s,%s)''',(code,category,Jsonb(detail or {})))
                await conn.execute('''UPDATE exchange_accounts SET state='INCIDENT',operator_enabled=FALSE,last_error=%s,
                                       updated_at=now() WHERE code=%s''',(category,code))

    async def open_incidents(self, code):
        async with self.pool.connection() as conn:
            cur = await conn.execute('''SELECT id,category,detail,created_at FROM exchange_incidents
                 WHERE account_code=%s AND acknowledged_at IS NULL
                 ORDER BY id DESC LIMIT 100''',(code,))
            return [dict(zip(('id','category','detail','created_at'),r)) for r in await cur.fetchall()]

    async def acknowledge(self, account, incident_id):
        async with self.pool.connection() as conn:
            cur = await conn.execute('''UPDATE exchange_incidents SET acknowledged_at=now()
              WHERE id=%s AND account_code=%s AND acknowledged_at IS NULL RETURNING id''',
              (incident_id,account))
            return await cur.fetchone() is not None

    async def count_today(self, account):
        async with self.pool.connection() as conn:
            row = await (await conn.execute('''SELECT COUNT(*) FROM exchange_order_intents
              WHERE account_code=%s AND created_at >= date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
              AND status NOT IN ('QUEUED','FAILED_POLICY')''',(account,))).fetchone()
            return row[0]

    async def cursors(self, code):
        async with self.pool.connection() as conn:
            cur = await conn.execute('''SELECT symbol,next_id FROM exchange_trade_cursors WHERE account_code=%s''',(code,))
            return dict(await cur.fetchall())

    async def add_fills(self, code, symbol, trades):
        if not trades:
            return
        async with self.pool.connection() as conn:
            async with conn.transaction():
                for trade in trades:
                    await conn.execute('''INSERT INTO exchange_fills(account_code,symbol,trade_id,
                      order_id,qty,price,commission,commission_asset,payload)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT DO NOTHING''',(code,symbol,int(trade['id']),str(trade['orderId']),
                      trade['qty'],trade['price'],trade.get('commission','0'),
                      trade.get('commissionAsset',''),Jsonb(trade)))
                max_id = max(int(t['id']) for t in trades) + 1
                await conn.execute('''INSERT INTO exchange_trade_cursors(account_code,symbol,next_id)
                  VALUES(%s,%s,%s) ON CONFLICT(account_code,symbol) DO UPDATE
                  SET next_id=GREATEST(exchange_trade_cursors.next_id,EXCLUDED.next_id),updated_at=now()''',
                  (code,symbol,max_id))

    async def stream_event(self, code, key, kind, event):
        async with self.pool.connection() as conn:
            await conn.execute('''INSERT INTO exchange_stream_events(account_code,event_key,event_type,payload)
             VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(code,key,kind,Jsonb(event)))
            await conn.execute('''UPDATE exchange_accounts SET last_stream_at=now() WHERE code=%s''',(code,))

    async def set_operator_enabled(self, code: str, enabled: bool):
        async with self.pool.connection() as conn:
            if enabled:
                cur = await conn.execute("""UPDATE exchange_accounts SET operator_enabled=TRUE
                  WHERE code=%s AND state='READY' AND NOT EXISTS (
                  SELECT 1 FROM exchange_incidents WHERE account_code=%s AND acknowledged_at IS NULL)
                  RETURNING code""", (code,code))
            else:
                cur = await conn.execute("UPDATE exchange_accounts SET operator_enabled=FALSE WHERE code=%s RETURNING code", (code,))
            return await cur.fetchone() is not None

    async def enabled(self, code: str) -> bool:
        async with self.pool.connection() as conn:
            row = await (await conn.execute("SELECT state,operator_enabled FROM exchange_accounts WHERE code=%s",(code,))).fetchone()
            return bool(row and row[0] == 'READY' and row[1])

    async def cancel_queued(self, code, id_):
        async with self.pool.connection() as conn:
            async with conn.transaction():
                row = await (await conn.execute("""UPDATE exchange_order_intents SET status='CANCELED',updated_at=now()
                    WHERE id=%s AND account_code=%s AND status='QUEUED' RETURNING id""",(str(id_),code))).fetchone()
                if row:
                    await conn.execute("INSERT INTO exchange_order_events(intent_id,event_kind) VALUES(%s,'CANCELED')",(str(id_),))
                return row is not None
