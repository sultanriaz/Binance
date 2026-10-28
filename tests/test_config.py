
import pytest

from trader.config import Settings


def configure(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "example-for-unit-tests-only-0123456789abcdef")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")


def test_defaults(monkeypatch):
    configure(monkeypatch)
    cfg = Settings.load()
    assert cfg.markets == ("spot", "futures")
    assert cfg.symbols == ("BTCUSDT", "ETHUSDT")


def test_live_trading_never_allowed(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    with pytest.raises(RuntimeError, match="READ ONLY"):
        Settings.load()


def test_invalid_symbols(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("SYMBOLS", "BTC-USDT")
    with pytest.raises(ValueError):
        Settings.load()


def test_invalid_markets(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("MARKETS", "spot,coinm")
    with pytest.raises(ValueError):
        Settings.load()


def test_missing_secret(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    with pytest.raises(ValueError):
        Settings.load()
