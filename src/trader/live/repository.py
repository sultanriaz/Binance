"""Production ledger, intents, approvals and incident controls (separate from sandbox)."""
from __future__ import annotations
from datetime import datetime,timezone
from uuid import uuid4
from psycopg.types.json import Jsonb
from trader.governance.core import digest
from trader.live.authorization import consume_authorization

TERMINAL={'FILLED','CANCELED','EXPIRED','REJECTED','FAILED_POLICY'}
ACTIVE={'SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED','CANCEL_PENDING'}
ALL=TERMINAL|ACTIVE|{'QUEUED'}

class LiveStateError(ValueError): pass

class LiveRepository:
    def __init__(self,db): self.db=db

    async def accounts(self):
        async with self.db.pool.connection() as conn:
            rows=await (await conn.execute('''SELECT code,state,armed,automation_enabled,last_reconciled_at,
                last_error,updated_at FROM live_accounts ORDER BY code''')).fetchall()
        keys=('code','state','armed','automation_enabled','last_reconciled_at','last_error','updated_at')
        return [dict(zip(keys,row)) for row in rows]

    async def get_account(self,code):
        async with self.db.pool.connection() as conn:
            row=await (await conn.execute('''SELECT code,state,armed,automation_enabled,
                last_reconciled_at,equity_baseline,equity_baseline_day,snapshot
                FROM live_accounts WHERE code=%s''',(code,))).fetchone()
        if row is None: return None
        return dict(zip(('code','state','armed','automation_enabled','last_reconciled_at',
                         'equity_baseline','equity_baseline_day','snapshot'),row))

    async def readiness(self,conn=None):
        """No self-attestation; P28 evidence must be independently recent and positive."""
        if conn is None:
            async with self.db.pool.connection() as c:
                return await self.readiness(c)
        row=await (await conn.execute('''SELECT verdict,evaluated_at FROM operations_readiness
                          ORDER BY id DESC LIMIT 1''')).fetchone()
        if not row or row[0]!='REVIEW_REQUIRED' or (datetime.now(timezone.utc)-row[1]).total_seconds()>1800:
            raise LiveStateError('FRESH_INDEPENDENT_P28_READINESS_REQUIRED')
        issues=await (await conn.execute('''SELECT
               (SELECT COUNT(*) FROM operations_incidents WHERE resolved_at IS NULL)+
               (SELECT COUNT(*) FROM exchange_incidents WHERE acknowledged_at IS NULL)+
               (SELECT COUNT(*) FROM exchange_order_intents WHERE status IN ('UNKNOWN','SUBMITTING','CANCEL_PENDING'))''')).fetchone()
        if issues[0]: raise LiveStateError('EXISTING_PLATFORM_INCIDENTS')

    async def arm(self,code,approval_id,actor):
        if code not in ('spot-live','futures-live'): raise LiveStateError('INVALID_ACCOUNT')
        kind='SPOT_ARM' if code=='spot-live' else 'FUTURES_ARM'
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                await self.readiness(conn)
                await consume_authorization(conn,approval_id,kind,actor,{'account':code})
                row=await (await conn.execute('''UPDATE live_accounts SET armed=TRUE,updated_at=now()
                   WHERE code=%s AND state='READY' AND last_reconciled_at>now()-interval '1 minute'
                   AND NOT EXISTS(SELECT 1 FROM live_incidents WHERE account_code=%s
                     AND acknowledged_at IS NULL) RETURNING code''',(code,code))).fetchone()
                if not row: raise LiveStateError('LIVE_RECONCILIATION_OR_INCIDENT_BLOCK')
        return True

    async def set_halt(self,code):
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("""UPDATE live_accounts SET armed=FALSE,automation_enabled=FALSE,
                    updated_at=now() WHERE code=%s""",(code,))
                # Halt must NOT leave stale approved opening orders ready for a later rearm.
                rows=await (await conn.execute("""UPDATE live_order_intents SET status='CANCELED',
                     updated_at=now(),error='CANCELED_ON_OPERATOR_HALT'
                     WHERE account_code=%s AND status='QUEUED' AND source!='PROTECTIVE'
                     RETURNING id""",(code,))).fetchall()
                for (id_,) in rows:
                    await conn.execute("""INSERT INTO live_order_events(intent_id,status,detail)
                        VALUES(%s,'CANCELED',%s)""",(id_,Jsonb({'reason':'OPERATOR_HALT'})))

    async def set_automation(self,approval_id,actor,config):
        payload={'account':'spot-live','config':config}
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                await self.readiness(conn)
                await consume_authorization(conn,approval_id,'SPOT_AUTOMATION',actor,payload)
                row=await (await conn.execute('''SELECT armed,state FROM live_accounts
                   WHERE code='spot-live' FOR UPDATE''')).fetchone()
                if row!=(True,'READY'):
                    raise LiveStateError('SPOT_ACCOUNT_MUST_BE_ARMED_AND_READY')
                await conn.execute('''INSERT INTO live_automation(account_code,symbol,quantity,
                  fast_window,slow_window,config_sha,last_candle) VALUES
                  ('spot-live',%s,%s,%s,%s,%s,0)
                  ON CONFLICT(account_code) DO UPDATE SET symbol=EXCLUDED.symbol,
                    quantity=EXCLUDED.quantity,fast_window=EXCLUDED.fast_window,
                    slow_window=EXCLUDED.slow_window,config_sha=EXCLUDED.config_sha,last_candle=0,
                    updated_at=now()''',
                  (config['symbol'],config['quantity'],config['fast_window'],
                   config['slow_window'],digest(payload)))
                await conn.execute("UPDATE live_accounts SET automation_enabled=TRUE WHERE code='spot-live'")
        return True

    async def enqueue(self,code,client_id,payload,approval_id,actor):
        if code not in ('spot-live','futures-live'): raise LiveStateError('INVALID_ACCOUNT')
        kind='SPOT_ORDER' if code=='spot-live' else 'FUTURES_ORDER'
        approved={'account':code,'client_order_id':client_id,'order':payload}
        order_id=str(uuid4())
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                await self.readiness(conn)
                await consume_authorization(conn,approval_id,kind,actor,approved)
                row=await (await conn.execute('''SELECT state,armed FROM live_accounts WHERE code=%s FOR UPDATE''',(code,))).fetchone()
                if row!=('READY',True): raise LiveStateError('LIVE_ACCOUNT_NOT_ARMED')
                if await self.has_unresolved(conn,code): raise LiveStateError('LIVE_ORDERS_OR_INCIDENTS_UNRESOLVED')
                await conn.execute('''INSERT INTO live_order_intents(id,account_code,client_order_id,
                    payload,payload_sha,source,authorization_id) VALUES(%s,%s,%s,%s,%s,'HUMAN',%s)''',
                    (order_id,code,client_id,Jsonb(payload),digest(payload),approval_id))
                await conn.execute("INSERT INTO live_order_events(intent_id,status) VALUES(%s,'QUEUED')",(order_id,))
                if code=='futures-live' and not payload.get('reduce_only'):
                    # Durable protective plan is created ATOMICALLY with entry intent.
                    protect=dict(symbol=payload['symbol'],side='SELL' if payload['side']=='BUY' else 'BUY',
                                 type='STOP_MARKET',quantity=payload['quantity'],
                                 trigger_price=payload['protective_stop'],reduce_only=True,
                                 parent_order_id=order_id)
                    stop_id=str(uuid4())
                    await conn.execute('''INSERT INTO live_order_intents(id,account_code,client_order_id,
                        payload,payload_sha,source) VALUES(%s,%s,%s,%s,%s,'PROTECTIVE')''',
                        (stop_id,code,'p'+client_id,Jsonb(protect),digest(protect)))
                    await conn.execute("INSERT INTO live_order_events(intent_id,status) VALUES(%s,'QUEUED')",(stop_id,))
        return {'id':order_id,'client_order_id':client_id,'status':'QUEUED'}

    async def has_unresolved(self,conn,code):
        row=await (await conn.execute('''SELECT
           EXISTS(SELECT 1 FROM live_incidents WHERE account_code=%s AND acknowledged_at IS NULL)
           OR EXISTS(SELECT 1 FROM live_order_intents WHERE account_code=%s
                     AND status NOT IN ('FILLED','CANCELED','EXPIRED','REJECTED','FAILED_POLICY'))''',
           (code,code))).fetchone()
        return bool(row[0])

    async def orders(self,code,limit=500):
        async with self.db.pool.connection() as conn:
            rows=await (await conn.execute('''SELECT id,client_order_id,payload,status,source,
               exchange_order_id,cancel_requested,created_at,error FROM live_order_intents
               WHERE account_code=%s ORDER BY created_at,id LIMIT %s''',(code,limit))).fetchall()
        keys=('id','client_order_id','payload','status','source','exchange_order_id','cancel_requested','created_at','error')
        return [dict(zip(keys,row)) for row in rows]

    async def status(self,id_,status,remote=None,err=None):
        if status not in ALL: raise ValueError('Unrecognized order status')
        remote_id = None
        if isinstance(remote,dict):
            raw = remote.get('algo',remote)
            remote_id = raw.get('algoId') or raw.get('orderId')
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("""UPDATE live_order_intents SET status=%s,updated_at=now(),
                    response=COALESCE(%s,response),exchange_order_id=COALESCE(%s,exchange_order_id),
                    error=%s WHERE id=%s""",
                    (status,Jsonb(remote) if remote is not None else None,
                     str(remote_id) if remote_id is not None else None,err,str(id_)))
                await conn.execute('''INSERT INTO live_order_events(intent_id,status,detail)
                    VALUES(%s,%s,%s)''',(str(id_),status,Jsonb(remote or ({'error':err} if err else {}))))

    async def claim(self,code,id_, *, protective=False):
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                row=await (await conn.execute('''SELECT i.id,i.client_order_id,i.payload,i.source
                    FROM live_order_intents i JOIN live_accounts a ON a.code=i.account_code
                    WHERE i.account_code=%s AND i.id=%s AND i.status='QUEUED'
                    AND a.state='READY' AND (a.armed OR i.source='PROTECTIVE')
                    AND NOT EXISTS(SELECT 1 FROM live_incidents li WHERE li.account_code=a.code
                        AND li.acknowledged_at IS NULL) FOR UPDATE OF i SKIP LOCKED''',
                    (code,str(id_)))).fetchone()
                if not row or (row[3]=='PROTECTIVE')!=protective:return None
                await conn.execute("UPDATE live_order_intents SET status='SUBMITTING' WHERE id=%s",(row[0],))
                await conn.execute("INSERT INTO live_order_events(intent_id,status) VALUES(%s,'SUBMITTING')",(row[0],))
                return dict(zip(('id','client_order_id','payload','source'),row))

    async def reconcile_record(self,code,snapshot):
        async with self.db.pool.connection() as conn:
            await conn.execute('''UPDATE live_accounts SET state='READY',last_reconciled_at=now(),
                snapshot=%s,last_error=NULL,updated_at=now() WHERE code=%s''',
                (Jsonb(snapshot),code))

    async def incident(self,code,reason,detail=None):
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute('''INSERT INTO live_incidents(account_code,category,detail)
                   VALUES(%s,%s,%s)''',(code,reason,Jsonb(detail or {})))
                await conn.execute('''UPDATE live_accounts SET state='INCIDENT',armed=FALSE,
                   automation_enabled=FALSE,last_error=%s WHERE code=%s''',(reason,code))

    async def ack(self,code,id_,operator):
        async with self.db.pool.connection() as conn:
            row=await (await conn.execute('''UPDATE live_incidents SET acknowledged_at=now(),
                acknowledged_by=%s WHERE account_code=%s AND id=%s AND acknowledged_at IS NULL
                RETURNING id''',(operator,code,id_))).fetchone()
            return row is not None

    async def incidents(self,code):
        async with self.db.pool.connection() as conn:
            rows=await (await conn.execute('''SELECT id,category,detail,raised_at FROM live_incidents
                 WHERE account_code=%s AND acknowledged_at IS NULL ORDER BY id DESC''',(code,))).fetchall()
        return [dict(zip(('id','category','detail','raised_at'),r)) for r in rows]

    async def recover(self):
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE live_order_intents SET status='UNKNOWN' WHERE status IN ('SUBMITTING','CANCEL_PENDING')")
                await conn.execute("UPDATE live_accounts SET state='HALTED',armed=FALSE,automation_enabled=FALSE,last_error='RESTART_REQUIRES_OPERATOR_REARM'")
                rows=await (await conn.execute("""UPDATE live_order_intents SET status='CANCELED',
                  error='STALE_ORDER_CANCELED_ON_RESTART' WHERE status='QUEUED'
                  AND source!='PROTECTIVE' RETURNING id""")).fetchall()
                for (id_,) in rows:
                    await conn.execute("""INSERT INTO live_order_events(intent_id,status,detail)
                       VALUES(%s,'CANCELED',%s)""",(id_,Jsonb({'reason':'RESTART_FENCE'})))

    async def fills(self,code,symbol,trades):
        if not trades:return
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                for t in trades:
                    await conn.execute('''INSERT INTO live_fills(account_code,symbol,trade_id,order_id,
                      quantity,price,fee,asset,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT DO NOTHING''',
                      (code,symbol,int(t['id']),str(t['orderId']),t['qty'],t['price'],
                       t.get('commission','0'),t.get('commissionAsset',''),Jsonb(t)))
                await conn.execute('''INSERT INTO live_fill_cursors(account_code,symbol,next_id)
                  VALUES(%s,%s,%s) ON CONFLICT(account_code,symbol) DO UPDATE SET
                  next_id=GREATEST(live_fill_cursors.next_id,EXCLUDED.next_id)''',
                  (code,symbol,max(int(t['id']) for t in trades)+1))

    async def fill_cursor(self,code,symbol):
        async with self.db.pool.connection() as conn:
            row=await (await conn.execute('''SELECT next_id FROM live_fill_cursors
              WHERE account_code=%s AND symbol=%s''',(code,symbol))).fetchone()
        return row[0] if row else None

    async def count_today(self,code):
        async with self.db.pool.connection() as conn:
            row=await (await conn.execute('''SELECT COUNT(*) FROM live_order_intents
              WHERE account_code=%s AND source!='PROTECTIVE' AND created_at>=date_trunc('day',now())
              AND status NOT IN ('QUEUED','FAILED_POLICY')''',(code,))).fetchone()
        return row[0]

    async def request_cancel(self,code,id_):
        async with self.db.pool.connection() as conn:
            row=await (await conn.execute('''UPDATE live_order_intents SET cancel_requested=TRUE
              WHERE account_code=%s AND id=%s AND status=ANY(%s) RETURNING id''',
              (code,str(id_),list(ACTIVE)))).fetchone()
            return row is not None

    async def cancel_queued(self,code,id_):
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                row=await (await conn.execute('''UPDATE live_order_intents SET status='CANCELED'
                  WHERE account_code=%s AND id=%s AND status='QUEUED' RETURNING id''',
                  (code,str(id_)))).fetchone()
                if row:
                    await conn.execute("INSERT INTO live_order_events(intent_id,status) VALUES(%s,'CANCELED')",(id_,))
                return row is not None
