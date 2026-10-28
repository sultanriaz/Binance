"""Pure, serializable paper-trading domain. All money uses Decimal strings."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal('0')
ONE = Decimal('1')


def dec(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError('finite decimal required')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('invalid decimal') from exc
    if not result.is_finite():
        raise ValueError('non-finite decimal')
    return result


def ds(value: Decimal) -> str:
    return format(value, 'f')


@dataclass(frozen=True)
class Quote:
    market: str
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_qty: Decimal
    ask_qty: Decimal
    ts_ms: int
    synthetic: bool = False

    def __post_init__(self):
        if self.market not in ('spot', 'futures') or not self.symbol.endswith('USDT'):
            raise ValueError('only spot/futures USDT instruments supported')
        if self.bid <= ZERO or self.ask < self.bid:
            raise ValueError('invalid crossed or non-positive quote')
        if self.bid_qty < ZERO or self.ask_qty < ZERO or self.ts_ms < 0:
            raise ValueError('invalid liquidity or timestamp')

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class Policy:
    max_order_notional: Decimal = Decimal('1000')
    max_position_fraction: Decimal = Decimal('0.30')
    max_gross_exposure_fraction: Decimal = Decimal('0.80')
    max_daily_drawdown_fraction: Decimal = Decimal('0.04')
    max_total_drawdown_fraction: Decimal = Decimal('0.15')
    max_leverage: int = 3
    min_notional: Decimal = Decimal('10')
    taker_fee_fraction: Decimal = Decimal('0.001')
    maker_fee_fraction: Decimal = Decimal('0.001')
    slippage_bps: Decimal = Decimal('2')
    participation_fraction: Decimal = Decimal('0.20')
    quote_max_age_ms: int = 15000

    def __post_init__(self):
        for name in ('max_position_fraction','max_gross_exposure_fraction',
                     'max_daily_drawdown_fraction','max_total_drawdown_fraction',
                     'participation_fraction'):
            value = getattr(self, name)
            if value <= ZERO or value > ONE:
                raise ValueError(f'{name} must be in (0,1]')
        for name in ('taker_fee_fraction','maker_fee_fraction'):
            if getattr(self, name) < ZERO or getattr(self, name) >= ONE:
                raise ValueError(f'invalid fee {name}')
        if self.max_leverage < 1 or self.max_leverage > 10:
            raise ValueError('excessive paper leverage')
        if self.min_notional <= ZERO or self.max_order_notional < self.min_notional:
            raise ValueError('invalid notional bounds')
        if self.slippage_bps < ZERO or self.slippage_bps > Decimal('200'):
            raise ValueError('unrealistic slippage config')
        if self.quote_max_age_ms < 1:
            raise ValueError('invalid quote TTL')


@dataclass
class Account:
    account_id: str
    initial_cash: Decimal
    cash: Decimal
    spot: dict[str, dict[str, str]] = field(default_factory=dict)
    futures: dict[str, dict[str, str]] = field(default_factory=dict)
    marks: dict[str, str] = field(default_factory=dict)
    orders: list[dict[str, Any]] = field(default_factory=list)
    ledger: list[dict[str, Any]] = field(default_factory=list)
    seen_quotes: dict[str, str] = field(default_factory=dict)
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    peak_equity: Decimal = ZERO
    day_start_equity: Decimal = ZERO
    day_key: str = ''
    paused: bool = False
    sequence: int = 0

    @classmethod
    def create(cls, account_id: str, initial_cash: Decimal) -> 'Account':
        if initial_cash <= ZERO:
            raise ValueError('initial virtual cash must be positive')
        return cls(account_id, initial_cash, initial_cash,
                   peak_equity=initial_cash, day_start_equity=initial_cash)

    def to_dict(self) -> dict[str, Any]:
        return {
            'account_id': self.account_id, 'initial_cash': ds(self.initial_cash),
            'cash': ds(self.cash), 'spot': self.spot, 'futures': self.futures,
            'marks': self.marks, 'orders': self.orders, 'ledger': self.ledger,
            'seen_quotes': self.seen_quotes,
            'realized_pnl': ds(self.realized_pnl), 'fees': ds(self.fees),
            'funding': ds(self.funding), 'peak_equity': ds(self.peak_equity),
            'day_start_equity': ds(self.day_start_equity), 'day_key': self.day_key,
            'paused': self.paused, 'sequence': self.sequence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'Account':
        return cls(account_id=d['account_id'], initial_cash=dec(d['initial_cash']),
                   cash=dec(d['cash']), spot=d.get('spot', {}),
                   futures=d.get('futures', {}), marks=d.get('marks', {}),
                   orders=d.get('orders', []), ledger=d.get('ledger', []),
                   seen_quotes=d.get('seen_quotes', {}),
                   realized_pnl=dec(d.get('realized_pnl', '0')),
                   fees=dec(d.get('fees', '0')), funding=dec(d.get('funding', '0')),
                   peak_equity=dec(d.get('peak_equity', d['initial_cash'])),
                   day_start_equity=dec(d.get('day_start_equity', d['initial_cash'])),
                   day_key=d.get('day_key', ''), paused=bool(d.get('paused', False)),
                   sequence=int(d.get('sequence', 0)))

    def equity(self) -> Decimal:
        result = self.cash
        for symbol, h in self.spot.items():
            result += dec(h['qty']) * dec(self.marks.get('spot:' + symbol, h['avg']))
        for symbol, p in self.futures.items():
            qty = dec(p['qty'])
            if qty:
                mark = dec(self.marks.get('futures:' + symbol, p['entry']))
                result += dec(p['margin']) + qty * (mark - dec(p['entry']))
        return result

    def gross_exposure(self) -> Decimal:
        total = ZERO
        for symbol, h in self.spot.items():
            total += abs(dec(h['qty'])) * dec(self.marks.get('spot:' + symbol, h['avg']))
        for symbol, p in self.futures.items():
            total += abs(dec(p['qty'])) * dec(self.marks.get('futures:' + symbol, p['entry']))
        return total

    def audit(self, kind: str, ts_ms: int, **details: Any) -> dict[str, Any]:
        self.sequence += 1
        item = {'seq': self.sequence, 'type': kind, 'ts_ms': ts_ms, **details}
        self.ledger.append(item)
        return item
