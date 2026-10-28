"""Fail-closed, environment-based configuration for the read-only foundation."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit
from dataclasses import dataclass

SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")
VALID_MARKETS = frozenset(("spot", "futures"))


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    nats_url: str
    jwt_secret: str
    markets: tuple[str, ...]
    symbols: tuple[str, ...]
    spot_rest: str
    futures_rest: str
    spot_wss: str
    futures_wss: str
    log_level: str

    @classmethod
    def load(cls) -> Settings:
        if os.getenv("ENABLE_LIVE_TRADING", "false").lower() != "false":
            raise RuntimeError("Module 1 is READ ONLY; ENABLE_LIVE_TRADING must be false")
        markets = tuple(dict.fromkeys(m.strip().lower() for m in os.getenv("MARKETS", "spot,futures").split(",") if m.strip()))
        symbols = tuple(dict.fromkeys(s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()))
        if not markets or not set(markets) <= VALID_MARKETS:
            raise ValueError("MARKETS must be a nonempty subset of spot,futures")
        if not symbols or len(symbols) > 8 or any(not SYMBOL_RE.fullmatch(s) for s in symbols):
            raise ValueError("SYMBOLS must contain 1–8 uppercase exchange symbols")
        secret = os.getenv("JWT_SECRET", "")
        if len(secret) < 32 or secret.startswith("REPLACE_"):
            raise ValueError("Set JWT_SECRET to a randomly generated secret, minimum 32 chars")
        database_url = os.getenv("DATABASE_URL", "postgresql://trader:local_dev_only@localhost:5432/trader")
        if os.getenv('DEPLOYMENT_MODE') in ('staging','production'):
            role = urlsplit(database_url).username or ''
            if role not in {'trader_gateway','trader_collector','trader_outbox',
                            'trader_paper','trader_research','trader_exchange','trader_operations','trader_live'}:
                raise RuntimeError('Staging requires a least-privilege database service identity')
        return cls(
            database_url=database_url,
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            nats_url=os.getenv("NATS_URL", "nats://localhost:4222"),
            jwt_secret=secret,
            markets=markets, symbols=symbols,
            spot_rest=os.getenv("BINANCE_SPOT_REST", "https://api.binance.com").rstrip("/"),
            futures_rest=os.getenv("BINANCE_FUTURES_REST", "https://fapi.binance.com").rstrip("/"),
            spot_wss=os.getenv("BINANCE_SPOT_WSS", "wss://stream.binance.com:9443").rstrip("/"),
            futures_wss=os.getenv("BINANCE_FUTURES_WSS", "wss://fstream.binance.com/market").rstrip("/"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
