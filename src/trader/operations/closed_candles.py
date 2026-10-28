"""Exactly-once, point-in-time admission of finalized public Binance candles."""
from __future__ import annotations

from trader.market import MarketEvent
from trader.paper.history import Bar


def closed_bar(event: MarketEvent) -> Bar | None:
    if event.event_type != 'kline' or event.payload.get('closed') is not True:
        return None
    p=event.payload
    if p.get('interval') != '1m':
        return None
    # Trust no live candle before Binance explicitly marks the candle closed.
    return Bar.parse({'market':event.market,'symbol':event.symbol,'interval':p['interval'],
                      'open_ms':p['open_ts'],'close_ms':p['close_ts'],
                      'o':p['open'],'h':p['high'],'l':p['low'],
                      'c':p['close'],'volume':p['volume']})
