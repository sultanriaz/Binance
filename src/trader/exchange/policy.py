"""Strict exchange-specific preflight for test orders. No account mutations."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


class PolicyError(ValueError):
    pass


def D(value) -> Decimal:
    try:
        d = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise PolicyError('INVALID_DECIMAL') from exc
    if not d.is_finite():
        raise PolicyError('NON_FINITE_NUMBER')
    return d


def quantize_down(value: Decimal | str, step: Decimal | str) -> Decimal:
    """Quantize a decimal strictly down to an exact multiple of step size."""
    v, s = D(value), D(step)
    if s <= 0:
        return v
    return (v // s) * s


def quantize_format(value: Decimal | str, step: Decimal | str) -> str:
    """Format decimal value matching the precision/step size string."""
    v, s = D(value), D(step)
    if s <= 0:
        return str(v)
    q = (v // s) * s
    s_str = format(s, 'f')
    if '.' in s_str:
        decimals = len(s_str.split('.')[1].rstrip('0'))
        return f"{q:.{decimals}f}"
    return str(int(q))


def validate_order(code: str, payload: dict, info: dict, quote: dict, snapshot: dict,
                   *, symbols: tuple[str, ...], max_notional: int, max_leverage: int):
    market = 'spot' if code == 'spot-testnet' else 'futures'
    sym = payload['symbol']
    if sym not in symbols:
        raise PolicyError('SYMBOL_NOT_ALLOWLISTED')
    instrument = next((s for s in info.get('symbols', []) if s.get('symbol') == sym), None)
    if not instrument or instrument.get('status') != 'TRADING':
        raise PolicyError('SYMBOL_NOT_TRADING')
    if instrument.get('quoteAsset') != 'USDT':
        raise PolicyError('ONLY_USDT_QUOTES_SUPPORTED')
    qty = D(payload['quantity'])
    if qty <= 0:
        raise PolicyError('NON_POSITIVE_QUANTITY')
    side = payload['side']
    if side not in ('BUY', 'SELL'):
        raise PolicyError('INVALID_SIDE')
    typ = payload['type']
    allowed = ('MARKET', 'LIMIT', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT') if market == 'spot' else (
        'MARKET', 'LIMIT', 'STOP_MARKET', 'TAKE_PROFIT_MARKET')
    if typ not in allowed:
        raise PolicyError('UNSUPPORTED_ORDER_TYPE')
    price = D(payload['price']) if payload.get('price') else D(quote['askPrice'] if side == 'BUY' else quote['bidPrice'])
    bid, ask = D(quote['bidPrice']), D(quote['askPrice'])
    if not (bid > 0 and ask >= bid and price > 0):
        raise PolicyError('INVALID_QUOTE')
    if (ask - bid) / ((ask + bid)/2) > Decimal('0.01'):
        raise PolicyError('SPREAD_EXCEEDS_ONE_PERCENT')
    if typ in ('LIMIT','STOP_LOSS_LIMIT','TAKE_PROFIT_LIMIT'):
        if not payload.get('price'):
            raise PolicyError('PRICE_REQUIRED')
        if not Decimal('.95') * bid <= price <= Decimal('1.05') * ask:
            raise PolicyError('LIMIT_TOO_FAR_FROM_MARKET')
    if typ in ('STOP_LOSS_LIMIT','TAKE_PROFIT_LIMIT','STOP_MARKET','TAKE_PROFIT_MARKET'):
        if not payload.get('trigger_price') or D(payload['trigger_price']) <= 0:
            raise PolicyError('TRIGGER_REQUIRED')
    trigger = D(payload['trigger_price']) if payload.get('trigger_price') else None
    if trigger and not Decimal('.95') * bid <= trigger <= Decimal('1.05') * ask:
        raise PolicyError('TRIGGER_TOO_FAR_FROM_MARKET')
    if typ in ('MARKET','STOP_MARKET','TAKE_PROFIT_MARKET') and payload.get('price'):
        raise PolicyError('UNEXPECTED_PRICE')
    if not typ.endswith('_LIMIT') and typ not in ('STOP_MARKET','TAKE_PROFIT_MARKET') and payload.get('trigger_price'):
        raise PolicyError('UNEXPECTED_TRIGGER')
    notional = qty * max(price, ask)
    if notional > D(max_notional):
        raise PolicyError('ORDER_NOTIONAL_LIMIT_EXCEEDED')
    filters = {f['filterType']: f for f in instrument.get('filters', [])}
    lot = filters.get('MARKET_LOT_SIZE' if typ in ('MARKET','STOP_MARKET','TAKE_PROFIT_MARKET') else 'LOT_SIZE') or filters.get('LOT_SIZE', {})
    step = D(lot.get('stepSize', '0'))
    if qty < D(lot.get('minQty', '0')) or qty > D(lot.get('maxQty', '999999999')) or (step and qty % step):
        raise PolicyError('QUANTITY_FILTER_REJECTED')
    pf = filters.get('PRICE_FILTER', {})
    tick = D(pf.get('tickSize', '0'))
    for p in [p for p in (D(payload['price']) if payload.get('price') else None, trigger) if p is not None]:
        if tick and p % tick:
            raise PolicyError('PRICE_TICK_FILTER_REJECTED')
    min_notional = D((filters.get('NOTIONAL') or filters.get('MIN_NOTIONAL') or {}).get('minNotional', '0'))
    if notional < min_notional:
        raise PolicyError('MIN_NOTIONAL_REJECTED')
    if market == 'spot':
        if payload.get('reduce_only'):
            raise PolicyError('SPOT_REDUCE_ONLY_UNSUPPORTED')
        balances = {a['asset']: D(a['free']) for a in snapshot['account'].get('balances', [])}
        if side == 'SELL' and balances.get(instrument['baseAsset'], D(0)) < qty:
            raise PolicyError('INSUFFICIENT_SPOT_INVENTORY')
        if side == 'BUY' and balances.get('USDT', D(0)) < qty * ask * Decimal('1.01'):
            raise PolicyError('INSUFFICIENT_SPOT_USDT')
    else:
        if snapshot.get('position_mode', {}).get('dualSidePosition') is not False:
            raise PolicyError('HEDGE_MODE_UNSUPPORTED')
        pos = next((p for p in snapshot['positions'] if p['symbol'] == sym and p.get('positionSide', 'BOTH') == 'BOTH'), None)
        if not pos or pos.get('marginType', '').lower() != 'isolated':
            raise PolicyError('ISOLATED_MARGIN_REQUIRED')
        if int(pos.get('leverage', 999)) > max_leverage:
            raise PolicyError('LEVERAGE_TOO_HIGH')
        old_qty = D(pos.get('positionAmt','0'))
        signed_change = qty if side == 'BUY' else -qty
        conditional = typ in ('STOP_MARKET','TAKE_PROFIT_MARKET')
        if conditional and not payload.get('reduce_only'):
            raise PolicyError('FUTURES_CONDITIONAL_MUST_REDUCE')
        if payload.get('reduce_only'):
            if not old_qty or old_qty * signed_change >= 0 or qty > abs(old_qty):
                raise PolicyError('REDUCE_ONLY_NOT_REDUCING_EXISTING_POSITION')
        else:
            if old_qty * signed_change < 0:
                raise PolicyError('REVERSAL_REQUIRES_REDUCE_ONLY_FIRST')
            # A market or limit entry must have enough margin and remain below a total notional cap.
            available = D(snapshot['account'].get('availableBalance','0'))
            if available < notional / D(max_leverage) * Decimal('1.2'):
                raise PolicyError('INSUFFICIENT_FUTURES_MARGIN_BUFFER')
            if (abs(old_qty) + qty) * ask > D(max_notional):
                raise PolicyError('POSITION_NOTIONAL_LIMIT_EXCEEDED')
    return {'symbol': sym, 'quantity': str(qty), 'estimated_notional': str(notional),
            'exchange': code, 'max_notional': str(max_notional)}
