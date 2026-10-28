"""Exchange-agnostic immutable event envelope and Binance stream parser."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    market: str
    symbol: str
    event_type: str
    exchange_ts: int | None
    received_ts: int
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return vars(self).copy()


class UnsupportedEvent(ValueError):
    pass


def decode(market: str, message: str | bytes, received_ts: int | None = None) -> MarketEvent:
    """Strictly normalize supported Binance WS events, preserving raw values as strings.

    Book-ticker events are top-of-book only. Full L2 reconstruction is deliberately
    out of scope of P08 and must not be inferred from the reported bid/ask.
    """
    raw = json.loads(message)
    if not isinstance(raw, dict):
        raise UnsupportedEvent("expected object")
    stream = str(raw.get("stream", ""))
    obj = raw.get("data", raw)
    if not isinstance(obj, dict):
        raise UnsupportedEvent("expected event object")
    symbol = str(obj.get("s", "")).upper()
    if not symbol or len(symbol) > 20:
        raise UnsupportedEvent("missing/invalid symbol")
    event = obj.get("e")
    if event == "aggTrade":
        kind = "agg_trade"
        payload = {"price": obj["p"], "quantity": obj["q"], "trade_id": obj["a"], "trade_ts": obj["T"]}
    elif event == "kline":
        kind = "kline"
        k = obj["k"]
        payload = {"interval": k["i"], "open_ts": k["t"], "close_ts": k["T"],
                   "open": k["o"], "high": k["h"], "low": k["l"], "close": k["c"],
                   "volume": k["v"], "closed": k["x"]}
    elif event == "markPriceUpdate" and market == "futures":
        kind = "mark_price"
        payload = {"mark_price": obj["p"], "index_price": obj["i"],
                   "funding_rate": obj.get("r", ""), "next_funding_ts": obj.get("T")}
    elif ("@bookTicker" in stream or ("b" in obj and "a" in obj and "u" in obj)):
        kind = "book_ticker"
        payload = {"best_bid": obj["b"], "bid_qty": obj["B"], "best_ask": obj["a"],
                   "ask_qty": obj["A"], "update_id": obj["u"]}
    else:
        raise UnsupportedEvent(f"unsupported stream event: {event}")
    ts = obj.get("E")
    if ts is not None and (not isinstance(ts, int) or ts < 0):
        raise ValueError("invalid exchange timestamp")
    time_ms = received_ts if received_ts is not None else int(time.time() * 1000)
    digest = hashlib.sha256(json.dumps({"market": market, "symbol": symbol, "kind": kind,
        "ts": ts, "payload": payload}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return MarketEvent(digest, market, symbol, kind, ts, time_ms, payload)


def stream_names(market: str, symbols: tuple[str, ...]) -> tuple[str, ...]:
    if market not in ("spot", "futures"):
        raise ValueError("invalid market")
    names: list[str] = []
    for sym in symbols:
        s = sym.lower()
        names.extend((f"{s}@aggTrade", f"{s}@kline_1m", f"{s}@bookTicker"))
        if market == "futures":
            names.append(f"{s}@markPrice@1s")
    return tuple(names)


def combined_url(base: str, market: str, symbols: tuple[str, ...]) -> str:
    from urllib.parse import quote
    streams = stream_names(market, symbols)
    return f"{base}/stream?streams=" + "/".join(quote(s, safe="@_") for s in streams)
