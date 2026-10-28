"""Pure spot/linear-futures paper fills. No broker API or live-order method exists."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from trader.paper.domain import Account, Policy, Quote, ZERO, ONE, dec, ds
from trader.paper.risk import RiskRejected, check


def mark(account: Account, quote: Quote, policy: Policy | None = None) -> None:
    """Mark-to-market; detect UTC-day boundary using equity before new day's activity."""
    day = datetime.fromtimestamp(quote.ts_ms / 1000, tz=timezone.utc).date().isoformat()
    if day != account.day_key:
        account.day_start_equity = account.equity()
        account.day_key = day
    account.marks[f'{quote.market}:{quote.symbol}'] = ds(quote.mid)
    # Simplified isolated futures liquidation: if equity in an isolated position
    # breaches a fixed maintenance requirement, the remaining collateral is lost.
    # This uses available marks only, NOT an exact Binance tiered liquidation model.
    if policy and quote.market == 'futures':
        position = account.futures.get(quote.symbol)
        if position and dec(position['qty']):
            signed = dec(position['qty'])
            collateral = dec(position['margin'])
            unrealized = signed * (quote.mid - dec(position['entry']))
            maintenance = abs(signed) * quote.mid * Decimal('.005')
            if collateral + unrealized <= maintenance:
                total_value = collateral + unrealized
                # Unrealized liquidation losses cannot withdraw funds that do not
                # exist: explicitly record any shortfall, and zero the position.
                remaining = max(ZERO, total_value - maintenance)
                shortfall = max(ZERO, maintenance - total_value)
                account.cash += remaining
                account.realized_pnl += unrealized
                account.audit('SIMULATED_LIQUIDATION', quote.ts_ms,
                    symbol=quote.symbol, signed_quantity=ds(signed),
                    mark_price=ds(quote.mid), unrealized_loss=ds(unrealized),
                    remaining_collateral=ds(remaining), shortfall=ds(shortfall))
                position.update(qty='0',entry='0',margin='0',leverage='1')
                # Cancel outstanding limit and protective orders on closed position.
                for o in account.orders:
                    if o['market']=='futures' and o['symbol']==quote.symbol and o['status'] in ('OPEN','PARTIALLY_FILLED'):
                        o['status']='CANCELLED'
                        o['updated_ts']=quote.ts_ms
    eq = account.equity()
    if eq > account.peak_equity:
        account.peak_equity = eq
    # Automatically stop opening new exposure after exceeding hard drawdown limits.
    if policy and eq > ZERO:
        if (account.peak_equity > ZERO and
                (account.peak_equity - eq) / account.peak_equity >= policy.max_total_drawdown_fraction):
            account.paused = True
        if (account.day_start_equity > ZERO and
                (account.day_start_equity - eq) / account.day_start_equity >= policy.max_daily_drawdown_fraction):
            account.paused = True
        if account.paused:
            for order in account.orders:
                if order['status'] in ('OPEN','PARTIALLY_FILLED') and not order['reduce_only'] and not (order['market']=='spot' and order['side']=='SELL'):
                    order['status']='PARTIALLY_FILLED_CANCELLED' if dec(order['filled']) else 'CANCELLED'
                    order['updated_ts']=quote.ts_ms
                    account.audit('RISK_CANCELLED_OPENING_ORDER',quote.ts_ms,order_id=order['id'])


def _fill(account: Account, order: dict, qty: Decimal, price: Decimal, fee: Decimal,
          ts_ms: int) -> dict:
    """Apply a fill as an atomic in-memory change; caller persists under DB row lock."""
    symbol = order['symbol']
    notional = qty * price
    side = order['side']
    if order['market'] == 'spot':
        h = account.spot.setdefault(symbol, {'qty': '0', 'avg': '0'})
        old = dec(h['qty'])
        if side == 'BUY':
            if account.cash < notional + fee:
                raise RiskRejected('FILL_INSUFFICIENT_CASH')
            h['avg'] = ds((old * dec(h['avg']) + notional) / (old + qty))
            h['qty'] = ds(old + qty)
            account.cash -= notional + fee
        else:
            if qty > old:
                raise RiskRejected('FILL_INSUFFICIENT_ASSET')
            account.cash += notional - fee
            account.realized_pnl += qty * (price - dec(h['avg']))
            h['qty'] = ds(old - qty)
            if old == qty:
                h['avg'] = '0'
    else:
        p = account.futures.setdefault(symbol, {'qty': '0', 'entry': '0', 'margin': '0', 'leverage': '1'})
        old = dec(p['qty'])
        delta = qty if side == 'BUY' else -qty
        if order['reduce_only']:
            if old == ZERO or old * delta >= ZERO or abs(delta) > abs(old):
                raise RiskRejected('REDUCE_ONLY_VIOLATION')
            released = dec(p['margin']) * qty / abs(old)
            pnl = (price - dec(p['entry'])) * qty * (ONE if old > 0 else -ONE)
            account.cash += released + pnl - fee
            account.realized_pnl += pnl
            p['margin'] = ds(dec(p['margin']) - released)
            p['qty'] = ds(old + delta)
            if dec(p['qty']) == ZERO:
                p['entry'], p['margin'], p['leverage'] = '0', '0', '1'
        else:
            if old and old * delta <= ZERO:
                raise RiskRejected('OPEN_DIRECTION_CONFLICT')
            leverage = int(order['leverage'])
            if old and int(p['leverage']) != leverage:
                raise RiskRejected('MIXED_LEVERAGE_NOT_SUPPORTED')
            required = notional / leverage
            if account.cash < required + fee:
                raise RiskRejected('FILL_INSUFFICIENT_MARGIN')
            new_qty = old + delta
            p['entry'] = ds((abs(old) * dec(p['entry']) + notional) / abs(new_qty))
            p['qty'] = ds(new_qty)
            p['margin'] = ds(dec(p['margin']) + required)
            p['leverage'] = str(leverage)
            account.cash -= required + fee
    if order['reduce_only'] or (order['market']=='spot' and side=='SELL'):
        remaining_position=(dec(account.spot.get(symbol,{}).get('qty','0'))
                            if order['market']=='spot' else abs(dec(account.futures.get(symbol,{}).get('qty','0'))))
        for protective in account.orders:
            if protective['id']==order['id'] or protective['market']!=order['market'] or protective['symbol']!=symbol:
                continue
            if protective['type'] not in ('STOP_MARKET','TAKE_PROFIT_MARKET') or protective['status'] not in ('OPEN','PARTIALLY_FILLED'):
                continue
            if remaining_position==ZERO:
                protective['status']='CANCELLED'
                protective['updated_ts']=ts_ms
            elif dec(protective['remaining'])>remaining_position:
                protective['remaining']=ds(remaining_position)
    account.fees += fee
    item = account.audit('FILL', ts_ms, order_id=order['id'], market=order['market'],
                         symbol=symbol, side=side, quantity=ds(qty), price=ds(price),
                         notional=ds(notional), fee=ds(fee), cash_after=ds(account.cash),
                         realized_pnl=ds(account.realized_pnl))
    return item


def _execute(account: Account, order: dict, quote: Quote, policy: Policy,
             ts_ms: int, crossed: bool = False) -> list[dict]:
    if order['status'] not in ('OPEN', 'PARTIALLY_FILLED'):
        return []
    side = order['side']
    reference = quote.ask if side == 'BUY' else quote.bid
    if order['type'] in ('STOP_MARKET', 'TAKE_PROFIT_MARKET'):
        trigger = dec(order['trigger_price'])
        triggered = ((reference <= trigger) if side == 'SELL' else (reference >= trigger)) if order['type']=='STOP_MARKET' else ((reference >= trigger) if side == 'SELL' else (reference <= trigger))
        if not triggered:
            return []
    limit = dec(order['limit_price']) if order['limit_price'] is not None else None
    if limit is not None and not crossed:
        if side == 'BUY' and reference > limit:
            return []
        if side == 'SELL' and reference < limit:
            return []
    volume = quote.ask_qty if side == 'BUY' else quote.bid_qty
    available = volume * policy.participation_fraction
    qty = min(dec(order['remaining']), available)
    if qty <= ZERO:
        return []
    if limit is not None:
        # Conservative limit-price assumption: no price improvement; touching a candle
        # is NOT a guaranteed exchange fill. Order-book fill also subject to participation.
        price = limit
    else:
        impact = policy.slippage_bps / Decimal('10000')
        price = reference * (ONE + impact if side == 'BUY' else ONE - impact)
        if price <= ZERO:
            raise RiskRejected('INVALID_SIMULATED_FILL_PRICE')
    is_maker = limit is not None and crossed
    fee = qty * price * (policy.maker_fee_fraction if is_maker else policy.taker_fee_fraction)
    fill = _fill(account, order, qty, price, fee, ts_ms)
    remaining = dec(order['remaining']) - qty
    order['remaining'] = ds(remaining)
    order['filled'] = ds(dec(order['filled']) + qty)
    order['status'] = 'FILLED' if remaining == ZERO else 'PARTIALLY_FILLED'
    order['updated_ts'] = ts_ms
    if order.get('oco_group') and dec(order['filled']) > ZERO:
        for sibling in account.orders:
            if sibling['id']!=order['id'] and sibling.get('oco_group')==order['oco_group'] and sibling['status'] in ('OPEN','PARTIALLY_FILLED'):
                sibling['status']='CANCELLED'
                sibling['updated_ts']=ts_ms
                account.audit('OCO_SIBLING_CANCELLED',ts_ms,order_id=sibling['id'],triggered_by=order['id'])
    return [fill]


def place_order(account: Account, quote: Quote, policy: Policy, *, side: str,
                quantity: Decimal, ts_ms: int, order_type: str = 'MARKET',
                limit_price: Decimal | None = None, leverage: int = 1,
                reduce_only: bool = False, client_id: str | None = None,
                trigger_price: Decimal | None = None, oco_group: str | None = None) -> dict:
    """Client idempotency applies within an account and survives serialization."""
    if client_id is None:
        client_id = str(uuid4())
    if not client_id or len(client_id) > 100:
        raise RiskRejected('INVALID_CLIENT_ID')
    for existing in account.orders:
        if existing['client_id'] == client_id:
            same = (existing['market']==quote.market and existing['symbol']==quote.symbol
                    and existing['side']==side and existing['type']==order_type
                    and dec(existing['quantity'])==quantity
                    and existing['leverage']==leverage and existing['reduce_only']==reduce_only
                    and existing['limit_price']==(ds(limit_price) if limit_price is not None else None)
                    and existing.get('trigger_price')==(ds(trigger_price) if trigger_price is not None else None)
                    and existing.get('oco_group')==oco_group)
            if not same:
                raise RiskRejected('CLIENT_ID_REUSE_CONFLICT')
            return existing  # no duplicate audit/fill
    conditional = order_type in ('STOP_MARKET','TAKE_PROFIT_MARKET')
    if conditional:
        if not reduce_only or trigger_price is None or trigger_price <= ZERO or limit_price is not None:
            raise RiskRejected('CONDITIONAL_MUST_BE_REDUCE_ONLY_WITH_TRIGGER')
        if side == 'SELL':
            correctly_placed = trigger_price < quote.bid if order_type=='STOP_MARKET' else trigger_price > quote.bid
        else:
            correctly_placed = trigger_price > quote.ask if order_type=='STOP_MARKET' else trigger_price < quote.ask
        if not correctly_placed:
            raise RiskRejected('CONDITIONAL_TRIGGER_WRONG_SIDE')
    elif trigger_price is not None:
        raise RiskRejected('TRIGGER_ONLY_FOR_CONDITIONAL_ORDER')
    if oco_group is not None and (not conditional or len(oco_group)>80 or not oco_group):
        raise RiskRejected('OCO_GROUP_REQUIRES_CONDITIONAL_ORDER')
    # Fresh mark BEFORE evaluating collateral and drawdown.
    mark(account, quote, policy)
    check(account, quote, policy, side, quantity, leverage, reduce_only,
          ts_ms, order_type, limit_price, oco_group)
    order = dict(id=str(uuid5(NAMESPACE_URL, account.account_id + ':' + client_id)), client_id=client_id, market=quote.market, symbol=quote.symbol,
                 side=side, type=order_type, quantity=ds(quantity), remaining=ds(quantity),
                 filled='0', limit_price=ds(limit_price) if limit_price is not None else None,
                 reference_price=ds(limit_price if limit_price is not None else
                                    (quote.ask if side == 'BUY' else quote.bid)),
                 leverage=leverage, reduce_only=reduce_only, status='OPEN',
                 trigger_price=ds(trigger_price) if trigger_price is not None else None,
                 oco_group=oco_group,
                 created_ts=ts_ms, updated_ts=ts_ms)
    account.orders.append(order)
    account.audit('ORDER_ACCEPTED', ts_ms, order_id=order['id'], client_id=client_id,
                  market=quote.market, symbol=quote.symbol, side=side,
                  quantity=ds(quantity), order_type=order_type)
    if not conditional:
        _execute(account, order, quote, policy, ts_ms)
    # MARKET is treated as immediate-or-cancel to avoid pretending to rest on an order book.
    if order_type == 'MARKET' and order['status'] != 'FILLED':
        order['status'] = 'PARTIALLY_FILLED_CANCELLED' if dec(order['filled']) else 'CANCELLED'
        order['updated_ts'] = ts_ms
        account.audit('ORDER_REMAINDER_CANCELLED', ts_ms, order_id=order['id'])
    mark(account, quote, policy)
    return order


def process_quote(account: Account, quote: Quote, policy: Policy) -> list[dict]:
    mark(account, quote, policy)
    fills: list[dict] = []
    bids_available = quote.bid_qty * policy.participation_fraction
    asks_available = quote.ask_qty * policy.participation_fraction
    for order in account.orders:
        if order['market'] != quote.market or order['symbol'] != quote.symbol:
            continue
        if order['type'] not in ('LIMIT','STOP_MARKET','TAKE_PROFIT_MARKET') or order['status'] not in ('OPEN','PARTIALLY_FILLED'):
            continue
        if order['side']=='BUY' and asks_available<=ZERO:
            continue
        if order['side']=='SELL' and bids_available<=ZERO:
            continue
        # _execute applies participation cap to displayed size, so pass the
        # remaining executable budget divided by participation_fraction.
        volume = (asks_available if order['side']=='BUY' else bids_available) / policy.participation_fraction
        budgeted = Quote(quote.market,quote.symbol,quote.bid,quote.ask,
                         volume if order['side']=='SELL' else ZERO,
                         volume if order['side']=='BUY' else ZERO,quote.ts_ms,quote.synthetic)
        new_fills=_execute(account,order,budgeted,policy,quote.ts_ms)
        fills.extend(new_fills)
        spent=sum((dec(item['quantity']) for item in new_fills),ZERO)
        if order['side']=='BUY':
            asks_available-=spent
        else:bids_available-=spent
    return fills


def process_bar(account: Account, *, market: str, symbol: str, open_price: Decimal,
                high: Decimal, low: Decimal, close: Decimal, volume: Decimal,
                ts_ms: int, policy: Policy, spread_bps: Decimal = Decimal('4')) -> list[dict]:
    """Historical replay: previously submitted orders only; fills on next candle.

    For limits, a high/low touch is only an approximate fill model. Uses candle
    volume participation cap, not verified live exchange queue-position data.
    """
    if not (ZERO < low <= min(open_price, close) <= max(open_price, close) <= high):
        raise ValueError('invalid OHLC')
    if volume < ZERO:
        raise ValueError('invalid candle volume')
    spread = spread_bps / Decimal('20000')
    quote = Quote(market, symbol, open_price * (ONE - spread),
                  open_price * (ONE + spread), volume / 2, volume / 2, ts_ms, True)
    mark(account, quote, policy)
    fills: list[dict] = []
    # No current-bar signal has been evaluated at this point.
    for order in sorted(account.orders, key=lambda o: (0 if o['type']=='STOP_MARKET' else 1)):
        if order['market'] != market or order['symbol'] != symbol:
            continue
        if order['status'] not in ('OPEN', 'PARTIALLY_FILLED') or order['created_ts'] >= ts_ms:
            continue
        if order['type']=='LIMIT':
            price=dec(order['limit_price'])
            touched=(low<=price) if order['side']=='BUY' else (high>=price)
            if touched:
                fills.extend(_execute(account,order,quote,policy,ts_ms,crossed=True))
        elif order['type'] in ('STOP_MARKET','TAKE_PROFIT_MARKET'):
            trigger=dec(order['trigger_price'])
            crossed = ((low<=trigger) if order['side']=='SELL' else (high>=trigger)) if order['type']=='STOP_MARKET' else ((high>=trigger) if order['side']=='SELL' else (low<=trigger))
            if crossed:
                trigger_quote=Quote(market,symbol,trigger,trigger,volume/2,volume/2,ts_ms,True)
                fills.extend(_execute(account,order,trigger_quote,policy,ts_ms))
    # Valuation uses closing price, but remains unavailable to signals until bar close.
    mark(account, Quote(market, symbol, close, close, volume, volume,
                        ts_ms + 1, True), policy)
    return fills


def apply_funding(account: Account, symbol: str, rate: Decimal, ts_ms: int) -> Decimal:
    p = account.futures.get(symbol)
    if p is None or dec(p['qty']) == ZERO:
        return ZERO
    mark_price = dec(account.marks.get('futures:' + symbol, p['entry']))
    # Positive funding means longs pay shorts.
    payment = -dec(p['qty']) * mark_price * rate
    account.cash += payment
    account.funding += payment
    account.audit('FUNDING', ts_ms, symbol=symbol, rate=ds(rate), amount=ds(payment))
    return payment


def cancel_order(account: Account, order_id: str, ts_ms: int) -> dict:
    order = next((o for o in account.orders if o['id'] == order_id), None)
    if order is None:
        raise ValueError('ORDER_NOT_FOUND')
    if order['status'] in ('OPEN','PARTIALLY_FILLED'):
        order['status'] = 'PARTIALLY_FILLED_CANCELLED' if dec(order['filled']) else 'CANCELLED'
        order['updated_ts'] = ts_ms
        account.audit('ORDER_CANCELLED', ts_ms, order_id=order_id)
    return order
