"""P29-P31 live execution engine. Exchange state is authoritative; never retry uncertain writes.

This engine is deliberately limited to single-writer, sequential canary exposure.
It is NOT a high-frequency system and cannot guarantee protective stops against gaps.
"""
from __future__ import annotations
from datetime import datetime,timezone
from decimal import Decimal
import logging
import time
from uuid import uuid4
from trader.exchange.engine import exchange_status
from trader.exchange.policy import D,PolicyError
from trader.exchange.transport import ExchangeError,UncertainDelivery
from trader.live.risk import validate_live_order,check_daily_drawdown,account_equity
from trader.live.repository import ACTIVE

LOG=logging.getLogger(__name__)

class LiveExecutionHalt(RuntimeError):pass

class LiveEngine:
    def __init__(self,code,adapter,repo,settings):
        self.code,self.adapter,self.repo,self.settings=code,adapter,repo,settings
        self.info=None
        self.info_ts=0
        import asyncio
        self.wakeup_event = asyncio.Event()

    async def _halt(self,reason,detail=None):
        await self.repo.incident(self.code,reason,detail)
        from trader.live.alerts import send_alert
        try:
            await send_alert(f"LIVE ENGINE HALT: {self.code}", f"Reason: {reason}", level="CRITICAL", metadata=detail)
        except Exception:
            pass
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

    async def _emergency_flatten_futures(self, symbol: str, unwind_side: str, qty: Decimal | str, reason: str):
        """Emergency watchdog unwind: Flatten unprotected futures position immediately."""
        unwind_client_id = f"e{uuid4().hex[:20]}"
        try:
            LOG.critical("EMERGENCY UNWIND: Flattening unprotected futures position %s %s %s reason=%s",
                         symbol, unwind_side, qty, reason)
            res = await self.adapter.submit({
                'symbol': symbol,
                'side': unwind_side,
                'type': 'MARKET',
                'quantity': str(qty),
                'reduce_only': True
            }, unwind_client_id)
            await self.repo.incident(self.code, 'EMERGENCY_UNWIND_SUBMITTED', {
                'symbol': symbol, 'side': unwind_side, 'qty': str(qty),
                'trigger_reason': reason, 'response': res
            })
            return res
        except Exception as exc:
            LOG.critical("EMERGENCY UNWIND FAILED: %s", str(exc))
            await self.repo.incident(self.code, 'EMERGENCY_UNWIND_FAILED', {
                'symbol': symbol, 'side': unwind_side, 'qty': str(qty),
                'trigger_reason': reason, 'error': str(exc)
            })
            return None

    async def _spot_equity(self,snapshot):
        account=snapshot['account']
        instruments=(await self._info())['symbols']
        bybase={x['baseAsset']:x['symbol'] for x in instruments if x.get('quoteAsset')=='USDT'
                and x['symbol'] in self.settings.symbols}
        all_usdt_pairs={x['baseAsset']:x['symbol'] for x in instruments if x.get('quoteAsset')=='USDT'}
        value=Decimal('0')
        for b in account.get('balances',[]):
            quantity=D(b['free'])+D(b['locked'])
            if quantity<=0:continue
            if b['asset']=='USDT':
                value+=quantity
            elif b['asset'] in bybase:
                quote=await self.adapter.quote(bybase[b['asset']])
                value+=quantity*D(quote['bidPrice'])
            elif b['asset']=='BNB':
                # BNB held for Binance fee discounts
                try:
                    quote=await self.adapter.quote('BNBUSDT')
                    value+=quantity*D(quote['bidPrice'])
                except Exception:
                    if quantity > Decimal('0.5'):
                        raise LiveExecutionHalt('SPOT_BNB_BALANCE_TOO_LARGE_TO_IGNORE')
            elif b['asset'] in all_usdt_pairs:
                # Value and filter negligible dust (< 1.00 USDT)
                try:
                    quote=await self.adapter.quote(all_usdt_pairs[b['asset']])
                    asset_val=quantity*D(quote['bidPrice'])
                    if asset_val < Decimal('1.00'):
                        continue
                except Exception:
                    pass
                if quantity < Decimal('0.0001'):
                    continue
                raise LiveExecutionHalt('SPOT_BALANCE_CANNOT_BE_VALUED_'+b['asset'])
            elif quantity < Decimal('0.0001'):
                continue
            else:
                raise LiveExecutionHalt('SPOT_BALANCE_CANNOT_BE_VALUED_'+b['asset'])
        return value

    async def initialize_futures_account(self):
        """Automatically ensure One-Way Mode, Isolated Margin, and bounded Leverage."""
        if self.code != 'futures-live':
            return
        # 1. Enforce One-Way Position Mode
        try:
            res = await self.adapter.set_one_way_mode()
            LOG.info("Futures account position mode initialized: %s", res.get('msg', 'OK'))
        except Exception as exc:
            LOG.warning("Failed to auto-set one-way mode: %s", exc)
        # 2. For each allowlisted symbol, ensure Isolated Margin and Max Leverage
        for sym in self.settings.symbols:
            try:
                res = await self.adapter.set_isolated_margin(sym)
                LOG.info("Futures margin type initialized for %s: %s", sym, res.get('msg', 'OK'))
            except Exception as exc:
                LOG.warning("Failed to auto-set isolated margin for %s: %s", sym, exc)
            try:
                res = await self.adapter.set_leverage(sym, self.settings.max_leverage)
                LOG.info("Futures leverage initialized for %s: %sx", sym, self.settings.max_leverage)
            except Exception as exc:
                LOG.warning("Failed to set leverage for %s: %s", sym, exc)

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

    async def panic_flatten(self):
        """Tier 2 Panic Kill-Switch: Cancel ALL exchange orders and market-close all positions/holdings."""
        from trader.live.alerts import send_alert
        LOG.critical("COMMENCING TIER 2 PANIC FLATTEN FOR %s", self.code)
        results = {'cancelled_symbols': [], 'flattened_positions': [], 'flattened_spot': []}
        for sym in self.settings.symbols:
            try:
                res = await self.adapter.cancel_all_symbol_orders(sym)
                results['cancelled_symbols'].append(sym)
                LOG.info("Panic flatten canceled open orders for %s: %s", sym, res)
            except Exception as exc:
                LOG.warning("Failed to cancel open orders for %s during panic flatten: %s", sym, exc)
        try:
            snapshot = await self.adapter.snapshot()
        except Exception as exc:
            LOG.critical("Failed to fetch snapshot during panic flatten: %s", exc)
            snapshot = {}
        if self.code == 'futures-live':
            for pos in snapshot.get('positions', []):
                amt = D(pos.get('positionAmt', '0'))
                if amt != 0:
                    unwind_side = 'SELL' if amt > 0 else 'BUY'
                    res = await self._emergency_flatten_futures(
                        pos['symbol'], unwind_side, abs(amt), 'TIER_2_PANIC_FLATTEN'
                    )
                    results['flattened_positions'].append({'symbol': pos['symbol'], 'qty': str(abs(amt)), 'side': unwind_side})
        elif self.code == 'spot-live':
            account = snapshot.get('account', {})
            balances = {b['asset']: D(b.get('free', '0')) for b in account.get('balances', [])}
            instruments = (await self._info())['symbols']
            bybase = {x['baseAsset']: x['symbol'] for x in instruments if x.get('quoteAsset') == 'USDT'
                      and x['symbol'] in self.settings.symbols}
            for asset, sym in bybase.items():
                qty = balances.get(asset, Decimal('0'))
                if qty > 0:
                    try:
                        quote = await self.adapter.quote(sym)
                        bid_price = D(quote['bidPrice'])
                        notional = qty * bid_price
                        if notional >= Decimal('5.0'):
                            from trader.exchange.policy import quantize_down
                            sym_info = next((s for s in instruments if s['symbol'] == sym), None)
                            lot_filter = next((f for f in sym_info['filters'] if f['filterType'] == 'LOT_SIZE'), None) if sym_info else None
                            step_size = lot_filter.get('stepSize', '0.0001') if lot_filter else '0.0001'
                            sell_qty = quantize_down(qty, step_size)
                            if D(sell_qty) > 0:
                                client_id = f"pf{uuid4().hex[:20]}"
                                res = await self.adapter.submit({
                                    'symbol': sym,
                                    'side': 'SELL',
                                    'type': 'MARKET',
                                    'quantity': str(sell_qty)
                                }, client_id)
                                results['flattened_spot'].append({'symbol': sym, 'qty': str(sell_qty)})
                                LOG.critical("Panic flatten market sold spot %s %s: %s", sym, sell_qty, res)
                    except Exception as exc:
                        LOG.critical("Failed to market sell spot %s during panic flatten: %s", sym, exc)
        await self.repo.incident(self.code, 'PANIC_FLATTEN_EXECUTED', results)
        try:
            await send_alert(
                title=f"PANIC FLATTEN EXECUTED: {self.code}",
                message=f"Tier 2 Panic Kill-Switch completed for {self.code}. Results: {results}",
                level="CRITICAL",
                metadata=results
            )
        except Exception:
            pass
        return results

    async def reconcile(self):
        if not self.settings.configured(self.code):
            raise LiveExecutionHalt('LIVE_WORKER_CONFIGURATION_DISABLED')
        # Check for unacknowledged concept drift interventions and halt
        async with self.repo.db.pool.connection() as conn:
            drift_incident = await (await conn.execute(
                "SELECT id, detail FROM live_incidents WHERE account_code=%s AND category='CONCEPT_DRIFT_INTERVENTION' AND acknowledged_at IS NULL LIMIT 1",
                (self.code,)
            )).fetchone()
            if drift_incident:
                await self._halt('CONCEPT_DRIFT_INTERVENTION_ACTIVE', drift_incident[1])
        account = await self.repo.get_account(self.code)
        if account and account.get('snapshot', {}).get('panic_flatten_pending'):
            LOG.critical("PANIC FLATTEN DETECTED FOR %s; EXECUTING FULL LIQUIDATION", self.code)
            await self.panic_flatten()
            async with self.repo.db.pool.connection() as conn:
                await conn.execute("""UPDATE live_accounts SET
                    snapshot = snapshot - 'panic_flatten_pending',
                    updated_at = now() WHERE code=%s""", (self.code,))
            await self._halt('PANIC_FLATTEN_COMPLETED')
        snapshot=await self.adapter.snapshot()
        if not isinstance(snapshot.get('orders'),list) or not isinstance(snapshot.get('algos'),list):
            await self._halt('INVALID_LIVE_EXCHANGE_SNAPSHOT')
        if self.code == 'spot-live':
            from trader.live.risk import check_bnb_fee_buffer
            fee_buffer = check_bnb_fee_buffer(snapshot.get('account', {}).get('balances', []))
            snapshot['bnb_fee_buffer'] = fee_buffer
            if not fee_buffer['sufficient_fee_buffer']:
                LOG.warning("Spot account BNB fee buffer warning: %s", fee_buffer['warning'])
        if self.code=='futures-live':

            if snapshot.get('position_mode',{}).get('dualSidePosition') is not False:
                try:
                    await self.adapter.set_one_way_mode()
                    snapshot = await self.adapter.snapshot()
                except Exception:
                    pass
                if snapshot.get('position_mode',{}).get('dualSidePosition') is not False:
                    await self._halt('FUTURES_HEDGE_MODE_UNSUPPORTED')
            from trader.live.risk import check_liquidation_proximity
            for pos in snapshot.get('positions', []):
                amt = D(pos.get('positionAmt', '0'))
                if amt != 0 and check_liquidation_proximity(pos, min_safe_distance=Decimal('0.03')):
                    unwind_side = 'SELL' if amt > 0 else 'BUY'
                    LOG.critical("LIQUIDATION WATCHDOG TRIGGERED for %s (posAmt: %s, mark: %s, liq: %s)",
                                 pos.get('symbol'), amt, pos.get('markPrice'), pos.get('liquidationPrice'))
                    await self._emergency_flatten_futures(
                        pos['symbol'], unwind_side, abs(amt),
                        f"LIQUIDATION_WATCHDOG: mark={pos.get('markPrice')} liq={pos.get('liquidationPrice')}"
                    )
                    await self._halt('LIQUIDATION_WATCHDOG_INTERVENED', {
                        'symbol': pos.get('symbol'), 'position': pos
                    })
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
        # Futures OCO bracket synchronization: if one bracket leg fills, cancel the counterpart
        if self.code == 'futures-live':
            for x in entries:
                if x['source'] == 'PROTECTIVE' and x['status'] == 'FILLED':
                    counterpart_id = x['payload'].get('counterpart_id')
                    if counterpart_id:
                        counterpart = next((o for o in entries if str(o['id']) == str(counterpart_id)), None)
                        if counterpart and counterpart['status'] in ACTIVE and not counterpart.get('cancel_requested'):
                            LOG.info("Bracket leg %s FILLED; auto-cancelling counterpart leg %s",
                                     x['payload'].get('bracket_role', x['id']), counterpart['id'])
                            await self.repo.request_cancel(self.code, counterpart['id'])
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
            resp = parent.get('response') or {}
            executed = D(resp.get('executedQty') or resp.get('cumQty', '0'))
            if parent['status'] in ('REJECTED','CANCELED','EXPIRED','FAILED_POLICY'):
                if executed <= 0:
                    await self.repo.status(plan['id'],'CANCELED',{'reason':'PARENT_NOT_FILLED'})
                    continue
                # If partial fill occurred before cancellation/expiration, protect the executed portion
                if str(executed) != plan['payload']['quantity']:
                    plan['payload']['quantity'] = str(executed)
                return await self._submit(plan,snapshot,protective=True)
            if parent['status'] in ('FILLED', 'PARTIALLY_FILLED'):
                if executed > 0 and str(executed) != plan['payload']['quantity']:
                    plan['payload']['quantity'] = str(executed)
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
            funding_info = None
            if self.code == 'futures-live' and not protective:
                try:
                    funding_info = await self.adapter.funding_rate(payload['symbol'])
                except Exception as exc:
                    LOG.warning("Funding rate query failed for %s: %s", payload['symbol'], exc)
            validate_live_order(self.code,payload,self.info,quote,fresh,self.settings,
                                open_incidents=len(await self.repo.incidents(self.code)),
                                funding_info=funding_info)
        except PolicyError as exc:
            if protective:
                await self._emergency_flatten_futures(
                    payload['symbol'], payload['side'], payload['quantity'],
                    f"PROTECTIVE_PREFLIGHT_FAILED: {exc}"
                )
                await self._halt('PROTECTIVE_PREFLIGHT_FAILED',{'reason':str(exc)})
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
            if protective:
                await self._emergency_flatten_futures(
                    payload['symbol'], payload['side'], payload['quantity'],
                    "PROTECTIVE_WRITE_UNCERTAIN"
                )
            await self._halt('ORDER_WRITE_UNCERTAIN',{'client_id':claimed['client_order_id']})
        except ExchangeError as exc:
            await self.repo.status(claimed['id'],'REJECTED',err=str(exc))
            if protective:
                await self._emergency_flatten_futures(
                    payload['symbol'], payload['side'], payload['quantity'],
                    f"PROTECTIVE_ORDER_REJECTED: {exc}"
                )
                await self._halt('PROTECTIVE_ORDER_REJECTED')
            return 'exchange_rejected'
        status=exchange_status(response,algo=self.adapter.is_algo(claimed['payload']))
        if status=='UNKNOWN':status='ACKNOWLEDGED'
        await self.repo.status(claimed['id'],status,response)
        if protective:
            try:
                verified=await self.adapter.query(payload['symbol'],claimed['client_order_id'],algo=True)
                verified_status=exchange_status(verified,algo=True)
                if verified_status not in ('NEW','ACKNOWLEDGED'):
                    await self._emergency_flatten_futures(
                        payload['symbol'], payload['side'], payload['quantity'],
                        f"PROTECTIVE_STOP_NOT_CONFIRMED: {verified_status}"
                    )
                    await self._halt('PROTECTIVE_STOP_NOT_CONFIRMED',{'status':verified_status})
                await self.repo.status(claimed['id'],verified_status,verified)
            except ExchangeError:
                await self._emergency_flatten_futures(
                    payload['symbol'], payload['side'], payload['quantity'],
                    "PROTECTIVE_STOP_CONFIRMATION_UNAVAILABLE"
                )
                await self._halt('PROTECTIVE_STOP_CONFIRMATION_UNAVAILABLE')
        # Immediate follow-up is mandatory for futures market entry. No further entry until
        # protection is placed and reconciled. Failures trigger emergency watchdog flatten.
        if self.code=='futures-live' and not protective and not payload.get('reduce_only'):
            updated=dict(entry)
            updated['status']=status
            if status not in ('FILLED','NEW','ACKNOWLEDGED','PARTIALLY_FILLED'):
                await self._halt('FUTURES_ENTRY_UNEXPECTED_ACK')
            try:
                parent=await self.adapter.query(payload['symbol'],claimed['client_order_id'])
                parent_status=exchange_status(parent)
                await self.repo.status(claimed['id'],parent_status,parent)
                executed_qty=D(parent.get('executedQty') or parent.get('cumQty', '0'))
                if parent_status not in ('FILLED', 'PARTIALLY_FILLED'):
                    if parent_status in ('CANCELED', 'EXPIRED', 'REJECTED') and executed_qty <= 0:
                        return 'submitted'
                    await self._halt('FUTURES_ENTRY_NEEDS_PROTECTION_REVIEW')
                plans=await self.repo.orders(self.code,limit=501)
                parent_plans=[x for x in plans if x['source']=='PROTECTIVE'
                              and x['payload'].get('parent_order_id')==str(claimed['id'])
                              and x['status']=='QUEUED']
                if not parent_plans:
                    await self._emergency_flatten_futures(
                        payload['symbol'], 'SELL' if payload['side']=='BUY' else 'BUY',
                        executed_qty or payload['quantity'], "FUTURES_PROTECTION_PLAN_MISSING"
                    )
                    await self._halt('FUTURES_PROTECTION_PLAN_MISSING')
                for plan in parent_plans:
                    # Adjust protective quantity to match actual executed quantity
                    if executed_qty > 0 and str(executed_qty) != plan['payload']['quantity']:
                        plan['payload']['quantity'] = str(executed_qty)
                        async with self.repo.db.pool.connection() as conn:
                            from psycopg.types.json import Jsonb
                            await conn.execute("""UPDATE live_order_intents
                                SET payload = jsonb_set(payload, '{quantity}', to_jsonb(%s::text))
                                WHERE id = %s""", (str(executed_qty), plan['id']))
                    await self._submit(plan,await self.adapter.snapshot(),protective=True)
            except (ExchangeError,UncertainDelivery) as exc:
                try:
                    await self._emergency_flatten_futures(
                        payload['symbol'], 'SELL' if payload['side']=='BUY' else 'BUY',
                        executed_qty if 'executed_qty' in locals() and executed_qty > 0 else payload['quantity'],
                        f"FUTURES_PROTECTION_VERIFICATION_FAILED: {exc}"
                    )
                except Exception:
                    pass
                await self._halt('FUTURES_PROTECTION_VERIFICATION_FAILED')
        return 'submitted'

