"""P29-P31 live execution engine. Exchange state is authoritative; never retry uncertain writes.

This engine is deliberately limited to single-writer, sequential canary exposure.
It is NOT a high-frequency system and cannot guarantee protective stops against gaps.
"""
from __future__ import annotations
from datetime import datetime,timezone
from decimal import Decimal
import time
from trader.exchange.engine import exchange_status
from trader.exchange.policy import D,PolicyError
from trader.exchange.transport import ExchangeError,UncertainDelivery
from trader.live.risk import validate_live_order,check_daily_drawdown,account_equity
from trader.live.repository import ACTIVE

class LiveExecutionHalt(RuntimeError):pass

class LiveEngine:
    def __init__(self,code,adapter,repo,settings):
        self.code,self.adapter,self.repo,self.settings=code,adapter,repo,settings
        self.info=None
        self.info_ts=0

    async def _halt(self,reason,detail=None):
        await self.repo.incident(self.code,reason,detail)
        raise LiveExecutionHalt(reason)

    async def _info(self):
        if self.info is None or time.monotonic()-self.info_ts>1800:
            self.info=await self.adapter.instruments()
            self.info_ts=time.monotonic()
        return self.info

    async def _observe(self,entry):
        payload=entry['payload']
        algo=self.adapter.is_algo(payload)
        try:
            remote=await self.adapter.query(payload['symbol'],entry['client_order_id'],algo=algo)
        except ExchangeError as exc:
            if exc.code in (-2011,-2013):return 'UNKNOWN'
            raise
        status=exchange_status(remote,algo=algo)
        if algo and remote.get('algoStatus') in ('TRIGGERED','FINISHED'):
            downstream_id=remote.get('actualOrderId')
            if not downstream_id:return 'UNKNOWN'
            try:
                downstream=await self.adapter.query_regular_by_id(payload['symbol'],int(downstream_id))
                status=exchange_status(downstream)
                remote={'algo':remote,'triggered_regular_order':downstream}
            except ExchangeError:return 'UNKNOWN'
        if status=='UNKNOWN':return status
        await self.repo.status(entry['id'],status,remote)
        return status

    async def _spot_equity(self,snapshot):
        account=snapshot['account']
        instruments=(await self._info())['symbols']
        bybase={x['baseAsset']:x['symbol'] for x in instruments if x.get('quoteAsset')=='USDT'
                and x['symbol'] in self.settings.symbols}
        value=Decimal('0')
        for b in account.get('balances',[]):
            quantity=D(b['free'])+D(b['locked'])
            if quantity==0:continue
            if b['asset']=='USDT':value+=quantity
            elif b['asset'] in bybase:
                quote=await self.adapter.quote(bybase[b['asset']])
                value+=quantity*D(quote['bidPrice'])
            else:
                raise LiveExecutionHalt('SPOT_BALANCE_CANNOT_BE_VALUED_'+b['asset'])
        return value

    async def _equity_guard(self,snapshot):
        equity=await self._spot_equity(snapshot) if self.code=='spot-live' else account_equity(self.code,snapshot)
        async with self.repo.db.pool.connection() as conn:
            async with conn.transaction():
                row=await (await conn.execute('''SELECT equity_baseline,equity_baseline_day
                  FROM live_accounts WHERE code=%s FOR UPDATE''',(self.code,))).fetchone()
                today=datetime.now(timezone.utc).date()
                if row[0] is None or row[1]!=today:
                    await conn.execute('''UPDATE live_accounts SET equity_baseline=%s,
                        equity_baseline_day=%s WHERE code=%s''',(equity,today,self.code))
                    base=equity
                else:base=row[0]
                check_daily_drawdown(equity,base,self.settings.max_daily_loss_pct)

    async def reconcile(self):
        if not self.settings.configured(self.code):
            raise LiveExecutionHalt('LIVE_WORKER_CONFIGURATION_DISABLED')
        snapshot=await self.adapter.snapshot()
        if not isinstance(snapshot.get('orders'),list) or not isinstance(snapshot.get('algos'),list):
            await self._halt('INVALID_LIVE_EXCHANGE_SNAPSHOT')
        if self.code=='futures-live' and snapshot.get('position_mode',{}).get('dualSidePosition') is not False:
            await self._halt('FUTURES_HEDGE_MODE_UNSUPPORTED')
        entries=await self.repo.orders(self.code,limit=501)
        if len(entries)>=501:await self._halt('LIVE_RECONCILIATION_HISTORY_LIMIT')
        ids={r['client_order_id'] for r in entries}
        untracked=[r for r in snapshot['orders'] if r.get('clientOrderId') not in ids]
        untracked += [r for r in snapshot['algos'] if r.get('clientAlgoId') not in ids]
        if untracked:
            await self._halt('UNMANAGED_LIVE_OPEN_ORDERS',{'count':len(untracked)})
        if self.code=='futures-live':
            history_symbols={x['payload']['symbol'] for x in entries if x['status']!='FAILED_POLICY'}
            unowned=[p['symbol'] for p in snapshot['positions'] if D(p.get('positionAmt','0'))!=0
                     and p['symbol'] not in history_symbols]
            if unowned:await self._halt('UNMANAGED_LIVE_FUTURES_POSITION',{'symbols':unowned})
        for x in entries:
            if x['status'] in ACTIVE:
                status=await self._observe(x)
                if status=='UNKNOWN':
                    await self._halt('UNCERTAIN_LIVE_ORDER_STATUS',{'client_order_id':x['client_order_id']})
        for symbol in sorted({x['payload']['symbol'] for x in entries}):
            cursor=await self.repo.fill_cursor(self.code,symbol)
            for page in range(4):
                trades=await self.adapter.trades(symbol,cursor)
                if not isinstance(trades,list):await self._halt('INVALID_LIVE_FILL_HISTORY')
                await self.repo.fills(self.code,symbol,trades)
                if not trades or len(trades)<500:break
                cursor=max(int(x['id']) for x in trades)+1
            else:await self._halt('LIVE_FILL_PAGINATION_INCOMPLETE',{'symbol':symbol})
        try:
            await self._equity_guard(snapshot)
        except (PolicyError,LiveExecutionHalt) as exc:
            await self._halt('LIVE_EQUITY_OR_DRAWDOWN_FAILURE',{'reason':str(exc)})
        if await self.repo.incidents(self.code):
            raise LiveExecutionHalt('OPEN_LIVE_INCIDENTS_BLOCK_TRADING')
        await self.repo.reconcile_record(self.code,snapshot)
        return snapshot

    async def process_once(self):
        snapshot=await self.reconcile()
        entries=await self.repo.orders(self.code,limit=501)
        # Protective orders always have priority, including after a worker restart or disarm.
        protective=[x for x in entries if x['source']=='PROTECTIVE' and x['status']=='QUEUED']
        for plan in protective:
            parent=next((x for x in entries if str(x['id'])==plan['payload']['parent_order_id']),None)
            if parent is None:await self._halt('PROTECTIVE_PARENT_MISSING')
            if parent['status'] in ('REJECTED','CANCELED','EXPIRED','FAILED_POLICY'):
                await self.repo.status(plan['id'],'CANCELED',{'reason':'PARENT_NOT_FILLED'})
                continue
            if parent['status']=='PARTIALLY_FILLED':
                await self._halt('PARTIAL_FUTURES_ENTRY_REQUIRES_MANUAL_PROTECTION')
            if parent['status']=='FILLED':
                return await self._submit(plan,snapshot,protective=True)
        # Cancels do not require permission to open risk, but unknown outcomes halt the account.
        for x in entries:
            if x['cancel_requested'] and x['status'] in ACTIVE:
                await self.repo.status(x['id'],'CANCEL_PENDING')
                try:
                    await self.adapter.cancel(x['payload']['symbol'],x['client_order_id'],
                                              algo=self.adapter.is_algo(x['payload']))
                except (ExchangeError,UncertainDelivery):
                    await self.repo.status(x['id'],'UNKNOWN',err='CANCEL_OUTCOME_UNCERTAIN')
                    await self._halt('CANCEL_OUTCOME_UNCERTAIN')
                return 'cancel_sent'
        account=await self.repo.get_account(self.code)
        if not account['armed'] or account['state']!='READY':return 'disarmed'
        await self.repo.readiness()
        if await self.repo.count_today(self.code)>=self.settings.max_daily_orders:
            return 'daily_limit'
        queued=[x for x in entries if x['status']=='QUEUED' and x['source']!='PROTECTIVE']
        if not queued:return 'idle'
        if any(x['status'] in ACTIVE and x['source']!='PROTECTIVE' for x in entries):
            return 'awaiting_previous_order'
        return await self._submit(queued[0],snapshot,protective=False)

    async def _submit(self,entry,snapshot,*,protective):
        payload=entry['payload']
        try:
            quote=await self.adapter.quote(payload['symbol'])
            # Fresh exchange API reads immediately before irreversible write.
            fresh=await self.adapter.snapshot()
            await self._info()
            validate_live_order(self.code,payload,self.info,quote,fresh,self.settings,
                                open_incidents=len(await self.repo.incidents(self.code)))
        except PolicyError as exc:
            if protective:await self._halt('PROTECTIVE_PREFLIGHT_FAILED',{'reason':str(exc)})
            await self.repo.status(entry['id'],'FAILED_POLICY',err=str(exc))
            return 'risk_rejected'
        claimed=await self.repo.claim(self.code,entry['id'],protective=protective)
        if not claimed:return 'claim_denied'
        if not protective:
            # One last read after taking the durable claim; a concurrent operator halt
            # may cancel entries before the HTTP request begins. Absolute race-free
            # cancellation cannot be guaranteed once a request is in flight.
            state=await self.repo.get_account(self.code)
            if not state or not state['armed'] or state['state']!='READY':
                await self.repo.status(claimed['id'],'CANCELED',err='OPERATOR_DISARMED_AFTER_CLAIM')
                return 'disarmed_after_claim'
        try:
            response=await self.adapter.submit(claimed['payload'],claimed['client_order_id'])
        except UncertainDelivery:
            await self.repo.status(claimed['id'],'UNKNOWN',err='ORDER_WRITE_UNCERTAIN')
            await self._halt('ORDER_WRITE_UNCERTAIN',{'client_id':claimed['client_order_id']})
        except ExchangeError as exc:
            await self.repo.status(claimed['id'],'REJECTED',err=str(exc))
            if protective:await self._halt('PROTECTIVE_ORDER_REJECTED')
            return 'exchange_rejected'
        status=exchange_status(response,algo=self.adapter.is_algo(claimed['payload']))
        if status=='UNKNOWN':status='ACKNOWLEDGED'
        await self.repo.status(claimed['id'],status,response)
        if protective:
            try:
                verified=await self.adapter.query(payload['symbol'],claimed['client_order_id'],algo=True)
                verified_status=exchange_status(verified,algo=True)
                if verified_status not in ('NEW','ACKNOWLEDGED'):
                    await self._halt('PROTECTIVE_STOP_NOT_CONFIRMED',{'status':verified_status})
                await self.repo.status(claimed['id'],verified_status,verified)
            except ExchangeError:
                await self._halt('PROTECTIVE_STOP_CONFIRMATION_UNAVAILABLE')
        # Immediate follow-up is mandatory for futures market entry. No further entry until
        # protection is placed and reconciled. Failures require emergency intervention.
        if self.code=='futures-live' and not protective and not payload.get('reduce_only'):
            updated=dict(entry)
            updated['status']=status
            if status not in ('FILLED','NEW','ACKNOWLEDGED'):
                await self._halt('FUTURES_ENTRY_UNEXPECTED_ACK')
            try:
                parent=await self.adapter.query(payload['symbol'],claimed['client_order_id'])
                parent_status=exchange_status(parent)
                await self.repo.status(claimed['id'],parent_status,parent)
                if parent_status!='FILLED':
                    await self._halt('FUTURES_ENTRY_NEEDS_PROTECTION_REVIEW')
                plans=await self.repo.orders(self.code,limit=501)
                plan=next((x for x in plans if x['source']=='PROTECTIVE'
                           and x['payload'].get('parent_order_id')==str(claimed['id'])),None)
                if not plan:await self._halt('FUTURES_PROTECTION_PLAN_MISSING')
                await self._submit(plan,await self.adapter.snapshot(),protective=True)
            except (ExchangeError,UncertainDelivery):
                await self._halt('FUTURES_PROTECTION_VERIFICATION_FAILED')
        return 'submitted'
