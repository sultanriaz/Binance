"""Deterministic SMA crossover research strategy; no LLM-issued trade orders."""
from __future__ import annotations

from decimal import Decimal
from trader.paper.domain import Account, ZERO, dec


def sma_signal(closes: list[Decimal], fast: int, slow: int) -> str:
    if fast < 2 or slow <= fast or slow > 300:
        raise ValueError('invalid moving average windows')
    if len(closes) < slow:
        return 'HOLD'
    quick = sum(closes[-fast:], ZERO) / fast
    long = sum(closes[-slow:], ZERO) / slow
    return 'LONG' if quick > long else 'FLAT'


def desired_order(a: Account, *, market: str, symbol: str, desired: str,
                  price: Decimal, allocation: Decimal) -> tuple[str, Decimal, bool] | None:
    if allocation <= ZERO or allocation > Decimal('.2'):
        raise ValueError('strategy allocation out of supported range')
    if desired not in ('LONG', 'FLAT'):
        return None
    if market == 'spot':
        held = dec(a.spot.get(symbol, {}).get('qty', '0'))
    else:
        held = dec(a.futures.get(symbol, {}).get('qty', '0'))
        if held < ZERO:
            return None  # do not interfere with manually opened short exposure
    if desired == 'FLAT':
        return ('SELL', held, True) if held > ZERO else None
    if held > ZERO:
        return None
    budget = min(a.equity() * allocation, Decimal('970'))
    if a.cash <= ZERO or price <= ZERO:
        return None
    # Fee and slippage buffer prevents paper cash overcommit.
    notional = min(budget, a.cash * Decimal('.98'))
    qty = (notional / price).quantize(Decimal('.00000001'))
    return ('BUY', qty, False) if qty > ZERO else None
