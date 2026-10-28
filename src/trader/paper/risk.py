"""Deterministic pre-trade policy. Reduction-only exits remain possible while paused."""
from __future__ import annotations

from decimal import Decimal
from trader.paper.domain import Account, Policy, Quote, ZERO, dec


class RiskRejected(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _pending_order_notional(a: Account) -> Decimal:
    return sum((dec(o['remaining']) * dec(o['reference_price'])
                for o in a.orders if o['status'] in ('OPEN', 'PARTIALLY_FILLED')
                and not o['reduce_only']), ZERO)


def check(a: Account, quote: Quote, policy: Policy, side: str, qty: Decimal,
          leverage: int, reduce_only: bool, ts_ms: int, order_type: str,
          limit_price: Decimal | None = None, oco_group: str | None = None) -> None:
    if side not in ('BUY', 'SELL') or order_type not in ('MARKET','LIMIT','STOP_MARKET','TAKE_PROFIT_MARKET'):
        raise RiskRejected('INVALID_ORDER')
    if qty <= ZERO:
        raise RiskRejected('NONPOSITIVE_QUANTITY')
    if order_type == 'LIMIT' and (limit_price is None or limit_price <= ZERO):
        raise RiskRejected('INVALID_LIMIT_PRICE')
    if abs(ts_ms - quote.ts_ms) > policy.quote_max_age_ms:
        raise RiskRejected('STALE_QUOTE')
    if leverage < 1 or leverage > policy.max_leverage:
        raise RiskRejected('LEVERAGE_LIMIT')
    if quote.market == 'spot' and leverage != 1:
        raise RiskRejected('SPOT_LEVERAGE_UNSUPPORTED')
    price = limit_price if limit_price is not None else (quote.ask if side == 'BUY' else quote.bid)
    notional = price * qty
    effective_reduce = reduce_only or (quote.market=='spot' and side=='SELL')
    if not effective_reduce and notional < policy.min_notional:
        raise RiskRejected('MIN_NOTIONAL')
    if not effective_reduce and notional > policy.max_order_notional:
        raise RiskRejected('MAX_ORDER_NOTIONAL')
    if effective_reduce:
        # OCO exits may share the same backing quantity; unrelated resting exits
        # cannot reserve more size than actually exists. Immediate manual exits
        # are allowed and will cancel obsolete protective orders after filling.
        if order_type in ('STOP_MARKET','TAKE_PROFIT_MARKET','LIMIT'):
            other_exits=[o for o in a.orders if o['market']==quote.market and
                o['symbol']==quote.symbol and o['status'] in ('OPEN','PARTIALLY_FILLED')
                and o['reduce_only'] and o['side']==side]
            reserved=sum((dec(o['remaining']) for o in other_exits if not oco_group or
                           o.get('oco_group')!=oco_group),ZERO)
        else:
            reserved=ZERO
        if quote.market == 'spot':
            if side != 'SELL' or qty + reserved > dec(a.spot.get(quote.symbol, {}).get('qty', '0')):
                raise RiskRejected('INVALID_SPOT_REDUCTION' if reduce_only else 'INSUFFICIENT_SPOT_ASSET')
        else:
            existing = dec(a.futures.get(quote.symbol, {}).get('qty', '0'))
            if not existing or (existing > 0 and side != 'SELL') or (existing < 0 and side != 'BUY') or qty + reserved > abs(existing):
                raise RiskRejected('INVALID_FUTURES_REDUCTION')
        return
    if a.paused:
        raise RiskRejected('TRADING_PAUSED')
    equity = a.equity()
    if equity <= ZERO:
        raise RiskRejected('NONPOSITIVE_EQUITY')
    if a.peak_equity > ZERO and (a.peak_equity - equity) / a.peak_equity >= policy.max_total_drawdown_fraction:
        raise RiskRejected('TOTAL_DRAWDOWN_LIMIT')
    if a.day_start_equity > ZERO and (a.day_start_equity - equity) / a.day_start_equity >= policy.max_daily_drawdown_fraction:
        raise RiskRejected('DAILY_DRAWDOWN_LIMIT')
    if quote.market=='spot':
        held_notional=dec(a.spot.get(quote.symbol, {}).get('qty','0'))*price
    else:
        held_notional=abs(dec(a.futures.get(quote.symbol, {}).get('qty','0')))*price
    pending_symbol=sum((dec(o['remaining'])*dec(o['reference_price']) for o in a.orders
        if o['market']==quote.market and o['symbol']==quote.symbol
        and o['status'] in ('OPEN','PARTIALLY_FILLED') and not o['reduce_only']),ZERO)
    if held_notional + pending_symbol + notional > equity * policy.max_position_fraction:
        raise RiskRejected('MAX_POSITION_FRACTION')
    if a.gross_exposure() + _pending_order_notional(a) + notional > equity * policy.max_gross_exposure_fraction:
        raise RiskRejected('GROSS_EXPOSURE_LIMIT')
    fee_upper = notional * max(policy.taker_fee_fraction, policy.maker_fee_fraction)
    if quote.market == 'spot':
        if side == 'BUY':
            reserved = sum((dec(o['remaining']) * dec(o['reference_price']) *
                            (1 + max(policy.taker_fee_fraction, policy.maker_fee_fraction))
                            for o in a.orders if o['market'] == 'spot' and o['side'] == 'BUY'
                            and o['status'] in ('OPEN','PARTIALLY_FILLED')), ZERO)
            if a.cash < reserved + notional + fee_upper:
                raise RiskRejected('INSUFFICIENT_VIRTUAL_CASH')
        else:
            owned = dec(a.spot.get(quote.symbol, {}).get('qty', '0'))
            reserved = sum((dec(o['remaining']) for o in a.orders
                            if o['market'] == 'spot' and o['symbol'] == quote.symbol
                            and o['side'] == 'SELL' and o['status'] in ('OPEN','PARTIALLY_FILLED')), ZERO)
            if qty > owned - reserved:
                raise RiskRejected('INSUFFICIENT_SPOT_ASSET')
    else:
        current = dec(a.futures.get(quote.symbol, {}).get('qty', '0'))
        direction = 1 if side == 'BUY' else -1
        if current and current * direction < 0:
            raise RiskRejected('FUTURES_REVERSAL_REQUIRES_REDUCE_ONLY')
        if current and int(a.futures[quote.symbol]['leverage']) != leverage:
            raise RiskRejected('MIXED_LEVERAGE_NOT_SUPPORTED')
        reserved = sum((dec(o['remaining']) * dec(o['reference_price']) / int(o['leverage'])
                        for o in a.orders if o['market'] == 'futures'
                        and o['status'] in ('OPEN','PARTIALLY_FILLED')
                        and not o['reduce_only']), ZERO)
        if a.cash < reserved + notional / leverage + fee_upper:
            raise RiskRejected('INSUFFICIENT_FUTURES_MARGIN')
