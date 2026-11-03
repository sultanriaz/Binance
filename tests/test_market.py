import json

import pytest

from trader.market import UnsupportedEvent, combined_url, decode, stream_names


@pytest.mark.parametrize("market,payload,kind", [
    ("spot", {"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "E": 1672515782136,
        "s": "BTCUSDT", "a": 200, "p": "120.20", "q": "0.12", "T": 1672515782130}}, "agg_trade"),
    ("futures", {"stream": "btcusdt@markPrice@1s", "data": {"e": "markPriceUpdate", "E": 1672515782136,
        "s": "BTCUSDT", "p": "120.20", "i": "120.10", "r": "0.0001", "T": 1672516000000}}, "mark_price"),
    ("spot", {"stream": "btcusdt@bookTicker", "data": {"u": 2, "s": "BTCUSDT", "b": "10", "B": "5", "a": "11", "A": "3"}}, "book_ticker"),
    ("spot", {"stream": "btcusdt@depth20@100ms", "data": {"lastUpdateId": 160,
        "bids": [["100.0", "5.0"]], "asks": [["101.0", "3.0"]]}}, "depth20"),
    ("futures", {"stream": "ethusdt@depth20@100ms", "data": {"e": "depthUpdate", "E": 1672515782136,
        "s": "ETHUSDT", "b": [["100.0", "5.0"]], "a": [["101.0", "3.0"]], "u": 160}}, "depth20"),
    ("futures", {"stream": "ethusdt@kline_1m", "data": {"e": "kline", "E": 1672515782136,
        "s": "ETHUSDT", "k": {"i": "1m", "t": 1672515780000, "T": 1672515839999,
        "o": "10", "h": "12", "l": "8", "c": "11", "v": "2", "x": False}}}, "kline"),
])
def test_parse_supported_events(market, payload, kind):
    a = decode(market, json.dumps(payload), received_ts=123456)
    b = decode(market, json.dumps(payload), received_ts=234567)
    assert a.event_type == kind
    assert a.event_id == b.event_id
    assert a.received_ts == 123456
    assert a.symbol in ("BTCUSDT", "ETHUSDT")
    assert isinstance(a.payload, dict)


def test_invalid_stream():
    with pytest.raises(UnsupportedEvent):
        decode("spot", '{"data":{}}')


def test_bad_time_rejected():
    with pytest.raises(ValueError):
        decode("spot", json.dumps({"e": "aggTrade", "E": "not-a-timestamp", "s": "BTCUSDT",
            "a": 1, "p": "1", "q": "1", "T": 1}))


def test_stream_urls_and_market_isolation():
    assert "@markPrice@1s" not in combined_url("wss://stream.binance.com:9443", "spot", ("BTCUSDT",))
    assert "btcusdt@markPrice@1s" in combined_url("wss://fstream.binance.com/market", "futures", ("BTCUSDT",))
    assert "btcusdt@depth20@100ms" in combined_url("wss://stream.binance.com:9443", "spot", ("BTCUSDT",))
    assert len(stream_names("spot", ("BTCUSDT", "ETHUSDT"))) == 8
    assert len(stream_names("futures", ("BTCUSDT", "ETHUSDT"))) == 10
    with pytest.raises(ValueError):
        stream_names("coin-m", ("BTCUSDT",))
