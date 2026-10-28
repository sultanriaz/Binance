"""P30 fixed-rule spot automation only; no agent or ML model can generate orders."""
from __future__ import annotations
from decimal import Decimal
from uuid import uuid4
from trader.paper.strategy import sma_signal
from trader.exchange.policy import D
from trader.governance.core import digest


def decide(closes: list[Decimal],fast:int,slow:int,held:Decimal) -> str | None:
    if len(closes)<slow:return None
    signal=sma_signal(closes,fast,slow)
    if signal=='LONG' and held<=0:return 'BUY'
    if signal=='FLAT' and held>0:return 'SELL'
    return None

async def maybe_schedule_spot(repo,settings):
    """Worker-only, read-only research input with transactional cursor/order creation."""
    from psycopg.types.json import Jsonb
    if not settings.configured('spot-live'):return 'disabled'
    async with repo.db.pool.connection() as conn:
        async with conn.transaction():
            account=await (await conn.execute('''SELECT state,armed,automation_enabled,snapshot FROM live_accounts
                  WHERE code='spot-live' FOR UPDATE''')).fetchone()
            if not account or account[:3]!=('READY',True,True):return 'disabled'
            await repo.readiness(conn)
            conf=await (await conn.execute('''SELECT symbol,quantity,fast_window,slow_window,last_candle,
                config_sha FROM live_automation WHERE account_code='spot-live' FOR UPDATE''')).fetchone()
            if not conf:return 'no_config'
            symbol,quantity,fast,slow,last,config_sha=conf
            if symbol not in settings.symbols:return 'invalid_symbol'
            bars=await (await conn.execute('''SELECT open_ms,close_ms,c FROM historical_bars
                 WHERE market='spot' AND symbol=%s AND interval='1m'
                 ORDER BY open_ms DESC LIMIT %s''',(symbol,slow+3))).fetchall()
            # Only closed bars, with a delay to avoid in-flight bar revisions.
            import time
            bars=sorted((int(o),int(e),D(c)) for o,e,c in bars if int(e)<int(time.time()*1000)-2000)
            if len(bars)<slow+2:return 'insufficient_candles'
            ms=bars[-1][0]
            if ms<=last:return 'already_processed'
            # Require strict 1m continuity. Never trade if gaps exist.
            if any(bars[i+1][0]-bars[i][0]!=60000 for i in range(len(bars)-1)):
                return 'candle_gap'
            balances={x['asset']:D(x['free']) for x in account[3]['account'].get('balances',[])}
            # Resolve the configured base asset from exchange's validated instruments metadata.
            instrument=await (await conn.execute('''SELECT base_asset FROM instruments
               WHERE market='spot' AND symbol=%s AND status='TRADING' ''',(symbol,))).fetchone()
            if not instrument:return 'missing_instrument'
            held=balances.get(instrument[0],Decimal(0))
            side=decide([c for _,_,c in bars],fast,slow,held)
            await conn.execute("UPDATE live_automation SET last_candle=%s WHERE account_code='spot-live'",(ms,))
            if side is None:return 'hold'
            if await repo.has_unresolved(conn,'spot-live'):return 'open_order_exists'
            today=await (await conn.execute('''SELECT COUNT(*) FROM live_order_intents
              WHERE account_code='spot-live' AND created_at>=date_trunc('day',now())
              AND source!='PROTECTIVE' AND status NOT IN ('QUEUED','FAILED_POLICY')''')).fetchone()
            if today[0]>=settings.max_daily_orders:return 'daily_limit'
            qty=D(quantity) if side=='BUY' else min(D(quantity),held)
            if qty<=0:return 'nothing_to_sell'
            proposed_notional=qty*bars[-1][2]
            if proposed_notional>settings.max_order_usdt:
                return 'configured_quantity_exceeds_cap'
            payload={'symbol':symbol,'side':side,'type':'MARKET','quantity':str(qty),'reduce_only':False}
            id_=str(uuid4())
            client=f'a{uuid4().hex[:25]}'
            await conn.execute('''INSERT INTO live_order_intents(id,account_code,client_order_id,
              payload,payload_sha,source) VALUES(%s,'spot-live',%s,%s,%s,'AUTOMATION')''',
              (id_,client,Jsonb(payload),digest(payload)))
            await conn.execute("INSERT INTO live_order_events(intent_id,status) VALUES(%s,'QUEUED')",(id_,))
            return side
