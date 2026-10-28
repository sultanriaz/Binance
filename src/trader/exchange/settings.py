"""Strictly pinned sandbox configuration; no caller-configurable hostname or live toggle."""
from __future__ import annotations

import os
from dataclasses import dataclass

HOSTS = {
    'spot-testnet': ('https://testnet.binance.vision', 'wss://ws-api.testnet.binance.vision/ws-api/v3'),
    'futures-demo': ('https://demo-fapi.binance.com', 'wss://demo-fstream.binance.com'),
}


@dataclass(frozen=True)
class ExchangeSettings:
    active: bool
    confirmed: bool
    symbols: tuple[str, ...]
    max_order_usdt: int
    max_daily_orders: int
    max_leverage: int
    spot_key: str
    spot_secret: str
    futures_key: str
    futures_secret: str
    poll_seconds: float

    @classmethod
    def load(cls):
        if os.getenv('ENABLE_LIVE_TRADING', 'false').lower() != 'false':
            raise RuntimeError('Production exchange trading is forbidden in Module 3')
        symbols = tuple(x.strip().upper() for x in os.getenv('TESTNET_SYMBOLS', 'BTCUSDT,ETHUSDT').split(',') if x.strip())
        import re
        if not symbols or len(symbols) > 6 or any(not re.fullmatch(r'[A-Z0-9]{5,20}',x) for x in symbols):
            raise ValueError('TESTNET_SYMBOLS must list 1–6 valid symbols')
        def bounded(name, default, low, high):
            n = int(os.getenv(name, str(default)))
            if not low <= n <= high:
                raise ValueError(f'{name} outside safe configured range')
            return n
        return cls(
            active=os.getenv('ENABLE_TESTNET_EXECUTION', 'false').lower() == 'true',
            confirmed=os.getenv('TESTNET_CONFIRM', '') == 'SANDBOX_ONLY_I_ACCEPT',
            symbols=symbols,
            max_order_usdt=bounded('TESTNET_MAX_ORDER_USDT', 50, 5, 500),
            max_daily_orders=bounded('TESTNET_MAX_DAILY_ORDERS', 20, 1, 100),
            max_leverage=bounded('TESTNET_MAX_LEVERAGE', 2, 1, 3),
            spot_key=os.getenv('BINANCE_SPOT_TESTNET_KEY', ''),
            spot_secret=os.getenv('BINANCE_SPOT_TESTNET_SECRET', ''),
            futures_key=os.getenv('BINANCE_FUTURES_DEMO_KEY', ''),
            futures_secret=os.getenv('BINANCE_FUTURES_DEMO_SECRET', ''),
            poll_seconds=float(os.getenv('EXCHANGE_POLL_SECONDS', '5')),
        )

    def configured(self, code: str) -> bool:
        if code not in HOSTS:
            return False
        key, secret = ((self.spot_key, self.spot_secret) if code == 'spot-testnet'
                       else (self.futures_key, self.futures_secret))
        return self.active and self.confirmed and bool(key and secret)
