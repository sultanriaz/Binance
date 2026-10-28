"""Fail-closed live configuration; hostnames are immutable and keys remain worker-only."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
import os
import re

LIVE_HOSTS = {'spot-live': 'https://api.binance.com', 'futures-live': 'https://fapi.binance.com'}

@dataclass(frozen=True)
class LiveSettings:
    enabled: bool
    confirmed: bool
    symbols: tuple[str, ...]
    max_order_usdt: Decimal
    max_daily_orders: int
    max_leverage: int
    max_daily_loss_pct: Decimal
    spot_key: str
    spot_secret: str
    futures_key: str
    futures_secret: str
    poll_seconds: float
    account_fingerprints: dict[str, str]

    @classmethod
    def load(cls) -> 'LiveSettings':
        enabled = os.getenv('LIVE_TRADING_ENABLED', 'false').lower() == 'true'
        confirmed = os.getenv('LIVE_TRADING_CONFIRM', '') == 'I_AUTHORIZE_CONTROLLED_REAL_MONEY_TRADING'
        if enabled and (not confirmed or os.getenv('DEPLOYMENT_MODE') != 'production'):
            raise RuntimeError('Live execution requires production deployment and exact authorization phrase')
        symbols = tuple(x.strip() for x in os.getenv('LIVE_SYMBOLS', 'BTCUSDT').split(',') if x.strip())
        if not symbols or len(symbols) > 3 or len(set(symbols)) != len(symbols) or any(
            re.fullmatch(r'[A-Z0-9]{5,20}', s) is None for s in symbols):
            raise ValueError('Invalid live instrument allowlist')
        max_order = Decimal(os.getenv('LIVE_MAX_ORDER_USDT', '25'))
        daily_loss = Decimal(os.getenv('LIVE_MAX_DAILY_LOSS_PCT', '1'))
        orders = int(os.getenv('LIVE_MAX_DAILY_ORDERS','5'))
        leverage = int(os.getenv('LIVE_MAX_LEVERAGE','2'))
        poll = float(os.getenv('LIVE_POLL_SECONDS','5'))
        if not (Decimal('5') <= max_order <= Decimal('100') and 1 <= orders <= 20
                and 1 <= leverage <= 2 and Decimal('0.1') <= daily_loss <= Decimal('2')
                and 2 <= poll <= 30):
            raise ValueError('Live safety limit exceeds built-in canary ceiling')
        return cls(enabled, confirmed, symbols,max_order,orders,leverage,daily_loss,
                   os.getenv('BINANCE_SPOT_LIVE_KEY',''), os.getenv('BINANCE_SPOT_LIVE_SECRET',''),
                   os.getenv('BINANCE_FUTURES_LIVE_KEY',''), os.getenv('BINANCE_FUTURES_LIVE_SECRET',''),
                   poll, {'spot-live':os.getenv('LIVE_SPOT_KEY_SHA256',''),
                          'futures-live':os.getenv('LIVE_FUTURES_KEY_SHA256','')})

    def configured(self, code: str) -> bool:
        import hashlib
        if not (self.enabled and self.confirmed and code in LIVE_HOSTS):
            return False
        key, secret = ((self.spot_key,self.spot_secret) if code == 'spot-live'
                       else (self.futures_key,self.futures_secret))
        expected = self.account_fingerprints.get(code,'')
        return bool(key and secret and re.fullmatch(r'[0-9a-f]{64}',expected)
                    and hashlib.sha256(key.encode()).hexdigest() == expected)
