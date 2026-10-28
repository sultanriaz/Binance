"""P15–P19: exchange snapshot reconciliation and fail-closed order processing.

A *single* runner holds a PostgreSQL session advisory lock. The ledger is an
observational audit; the exchange, not locally inferred trades, owns balances.
"""
from __future__ import annotations

import logging
import time

from trader.exchange.policy import D, PolicyError, validate_order
from trader.exchange.repository import ACTIVE, TERMINAL
from trader.exchange.transport import ExchangeError, UncertainDelivery

LOG = logging.getLogger(__name__)


class ReconcileError(Exception):
    pass


def exchange_status(raw: dict, *, algo=False):
    s = raw.get('algoStatus' if algo else 'status', 'UNKNOWN')
    return {
        'NEW':'NEW', 'PENDING_NEW':'ACKNOWLEDGED', 'PARTIALLY_FILLED':'PARTIALLY_FILLED',
        'FILLED':'FILLED', 'CANCELED':'CANCELED', 'CANCELLED':'CANCELED',
        'EXPIRED':'EXPIRED', 'EXPIRED_IN_MATCH':'EXPIRED', 'REJECTED':'REJECTED',
        'TRIGGERED':'UNKNOWN', 'FINISHED':'UNKNOWN',
    }.get(s, 'UNKNOWN')


def remote_client_id(record: dict, *, algo=False):
    return record.get('clientAlgoId' if algo else 'clientOrderId')


class SandboxEngine:
    def __init__(self, code, adapter, repo, settings):
        self.code, self.adapter, self.repo, self.settings = code, adapter, repo, settings
        self.last_info = None
        self.last_info_ts = 0
        self._last_incident = None

    async def _info(self):
        if self.last_info is None or time.monotonic() - self.last_info_ts > 3600:
            self.last_info = await self.adapter.instruments()
            self.last_info_ts = time.monotonic()
        return self.last_info

    async def _inc(self, category, detail=None):
        if self._last_incident != category:
            await self.repo.incident(self.code, category, detail=detail)
            self._last_incident = category
        raise ReconcileError(category)

    async def _observe(self, item):
        payload = item['payload']
        algo = self.adapter.is_algo(payload)
        try:
            remote = await self.adapter.query(payload['symbol'], item['client_order_id'], algo=algo)
        except ExchangeError as exc:
            if exc.code in (-2013, -2011):
                # Absence is not proof of non-execution; the fills/visibility may be delayed.
                return 'UNKNOWN'
            raise
        status = exchange_status(remote, algo=algo)
        if algo and remote.get('algoStatus') in ('TRIGGERED','FINISHED'):
            actual_id = remote.get('actualOrderId')
            if actual_id:
                try:
                    downstream = await self.adapter.query_regular_by_id(payload['symbol'],int(actual_id))
                    status = exchange_status(downstream)
                    remote = {'algo':remote,'triggered_regular_order':downstream}
                except ExchangeError:
                    status = 'UNKNOWN'
            else:
                status = 'UNKNOWN'
        if status == 'UNKNOWN':
            return 'UNKNOWN'
        await self.repo.set_status(item['id'], status, remote,
                                   order_id=str(remote.get('algo',remote).get('algoId' if algo else 'orderId','')))
        return status

    async def reconcile(self):
        """Read authoritative balances, orders, algos, fills, positions; fail closed on gaps."""
        await self.repo.mark_account(self.code, 'RECONCILING')
        try:
            snapshot = await self.adapter.snapshot()
            if not isinstance(snapshot['orders'], list) or not isinstance(snapshot['algos'], list):
                await self._inc('INVALID_EXCHANGE_SNAPSHOT')
            if self.code == 'futures-demo' and snapshot['position_mode'].get('dualSidePosition') is not False:
                await self._inc('UNSUPPORTED_FUTURES_HEDGE_MODE')
            local = await self.repo.intents(self.code, limit=2000)
            if len(local) == 2000:
                await self._inc('RECONCILIATION_HISTORY_LIMIT_REACHED')
            # Scan open orders first; any unmatched exchange activity is an incident.
            local_ids = {x['client_order_id'] for x in local}
            orphans = [x for x in snapshot['orders'] if remote_client_id(x) not in local_ids]
            orphans += [x for x in snapshot['algos'] if remote_client_id(x, algo=True) not in local_ids]
            if orphans:
                await self._inc('UNMANAGED_EXCHANGE_OPEN_ORDERS',
                                {'count':len(orphans), 'symbols': sorted({str(x.get('symbol')) for x in orphans})})
            # A position created outside our order history requires operator intervention.
            if self.code == 'futures-demo':
                symbols_with_history = {x['payload']['symbol'] for x in local if x['status'] != 'FAILED_POLICY'}
                unmanaged = [p['symbol'] for p in snapshot['positions']
                             if D(p.get('positionAmt', '0')) != 0 and p['symbol'] not in symbols_with_history]
                if unmanaged:
                    await self._inc('UNMANAGED_FUTURES_POSITION', {'symbols':unmanaged})
            # Query unsettled statuses by stable client IDs rather than guessing from streams.
            unresolved = [i for i in local if i['status'] not in TERMINAL and i['status'] != 'QUEUED']
            for item in unresolved:
                if await self._observe(item) == 'UNKNOWN':
                    await self._inc('AMBIGUOUS_ORDER_OUTCOME', {'client_id':item['client_order_id']})
            # Account-specific fill pagination, scoped only to configured / managed symbols.
            cursors = await self.repo.cursors(self.code)
            active_symbols = {x['payload']['symbol'] for x in local}
            active_symbols |= {x['symbol'] for x in snapshot['orders']}
            active_symbols |= {x['symbol'] for x in snapshot['algos']}
            for symbol in sorted(active_symbols):
                next_id = cursors.get(symbol)
                for page in range(6):
                    fills = await self.adapter.trades(symbol, next_id)
                    if not isinstance(fills, list):
                        await self._inc('INVALID_TRADE_HISTORY')
                    if fills:
                        await self.repo.add_fills(self.code, symbol, fills)
                        next_id = max(int(t['id']) for t in fills) + 1
                    if len(fills) < 500:
                        break
                else:
                    await self._inc('TRADE_HISTORY_BACKFILL_INCOMPLETE', {'symbol':symbol})
            if await self.repo.open_incidents(self.code):
                await self.repo.mark_account(self.code, 'INCIDENT', error='OPEN_CRITICAL_INCIDENTS_REQUIRE_REVIEW')
                raise ReconcileError('OPEN_CRITICAL_INCIDENTS_REQUIRE_REVIEW')
            await self.repo.mark_account(self.code, 'READY', snapshot=snapshot)
            self._last_incident = None
            return snapshot
        except ReconcileError:
            raise
        except (ExchangeError, ValueError, KeyError, TypeError) as exc:
            await self.repo.mark_account(self.code, 'HALTED', error=type(exc).__name__)
            raise ReconcileError('EXCHANGE_RECONCILIATION_FAILED') from exc

    async def _preflight(self, payload, snapshot):
        if not self.settings.configured(self.code):
            raise PolicyError('TESTNET_EXECUTION_DISABLED')
        if await self.repo.count_today(self.code) >= self.settings.max_daily_orders:
            raise PolicyError('DAILY_TEST_ORDER_LIMIT')
        info = await self._info()
        quote = await self.adapter.quote(payload['symbol'])
        return validate_order(self.code, payload, info, quote, snapshot,
                              symbols=self.settings.symbols,
                              max_notional=self.settings.max_order_usdt,
                              max_leverage=self.settings.max_leverage)

    async def process_once(self):
        """Reconcile before EACH new trade; no write if state uncertain or snapshot invalid."""
        snapshot = await self.reconcile()
        local = await self.repo.intents(self.code, limit=2000)
        # Cancel existing orders explicitly requested by the administrator.
        for order in local:
            if order['cancel_requested'] and order['status'] in ACTIVE:
                algo = self.adapter.is_algo(order['payload'])
                await self.repo.set_status(order['id'], 'CANCEL_PENDING')
                try:
                    result = await self.adapter.cancel(order['payload']['symbol'],order['client_order_id'],algo=algo)
                    await self.repo.set_status(order['id'],'CANCEL_PENDING',result)
                except (ExchangeError, UncertainDelivery):
                    await self.repo.set_status(order['id'],'UNKNOWN',err='CANCEL_OUTCOME_UNCERTAIN')
                    await self._inc('CANCEL_OUTCOME_UNCERTAIN', {'client_id':order['client_order_id']})
                return 'cancelled'
        if not await self.repo.enabled(self.code):
            return 'disarmed'
        queued = [x for x in local if x['status'] == 'QUEUED']
        if not queued:
            return 'idle'
        # Do not open a new position while other active orders are still outstanding.
        item = sorted(queued, key=lambda x: x['created_at'])[0]
        open_entries = [x for x in local if x['status'] in ACTIVE and not x['payload'].get('reduce_only')]
        if open_entries and not item['payload'].get('reduce_only'):
            return 'waiting_for_open_order'
        try:
            await self._preflight(item['payload'], snapshot)
        except PolicyError as exc:
            await self.repo.set_status(item['id'], 'FAILED_POLICY', err=str(exc))
            return 'policy_rejected'
        # The claim transaction writes SUBMITTING *before* any HTTP write attempt.
        claim = await self.repo.claim_queued(self.code, item['id'])
        if claim is None:
            return 'already_claimed'
        try:
            response = await self.adapter.submit(claim['payload'], claim['client_order_id'])
        except UncertainDelivery:
            await self.repo.set_status(claim['id'],'UNKNOWN',err='UNDETERMINED_SUBMISSION')
            await self._inc('UNDETERMINED_SUBMISSION', {'client_id':claim['client_order_id']})
        except ExchangeError as exc:
            # Explicit 4xx rejection only; treat it as a rejected exchange order.
            await self.repo.set_status(claim['id'],'REJECTED',err=str(exc))
            return 'exchange_rejected'
        status = exchange_status(response, algo=self.adapter.is_algo(claim['payload']))
        if status == 'UNKNOWN':
            status = 'ACKNOWLEDGED'  # ACK payload may not include status; next read verifies it.
        await self.repo.set_status(claim['id'],status,response,
                                   order_id=str(response.get('algoId',response.get('orderId',''))))
        # Do not claim final fills until next authoritative reconcile.
        return 'submitted'
