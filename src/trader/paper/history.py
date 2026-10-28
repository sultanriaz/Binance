"""Binance public kline import and hash-verified deterministic JSONL replay.

Use CLI `download` for public candles and `load` for PostgreSQL import. No API keys.
Canonical JSONL saves decimal prices as strings, timestamps in milliseconds.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx
from trader.paper.domain import dec, ds

INTERVAL_MS = {'1m':60000, '5m':300000, '15m':900000, '1h':3600000, '4h':14400000, '1d':86400000}
BASES = {'spot':'https://data-api.binance.vision', 'futures':'https://fapi.binance.com'}
PATHS = {'spot':'/api/v3/klines', 'futures':'/fapi/v1/klines'}


@dataclass(frozen=True)
class Bar:
    market: str
    symbol: str
    interval: str
    open_ms: int
    close_ms: int
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal
    volume: Decimal

    @classmethod
    def parse(cls, row: dict) -> 'Bar':
        try:
            bar = cls(row['market'], row['symbol'], row['interval'],
                      int(row['open_ms']), int(row['close_ms']),
                      *[dec(row[k]) for k in ('o','h','l','c','volume')])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('invalid historical bar') from exc
        if bar.market not in BASES or not bar.symbol.endswith('USDT') or bar.interval not in INTERVAL_MS:
            raise ValueError('unsupported bar instrument or interval')
        if (bar.open_ms < 0 or bar.open_ms % INTERVAL_MS[bar.interval] or
                bar.close_ms < bar.open_ms or bar.close_ms >= bar.open_ms + INTERVAL_MS[bar.interval] or
                bar.l <= 0 or bar.l > min(bar.o, bar.c) or bar.h < max(bar.o, bar.c) or
                bar.volume < 0):
            raise ValueError('invalid candle boundaries/OHLC')
        return bar

    def as_dict(self) -> dict:
        return {'market':self.market, 'symbol':self.symbol, 'interval':self.interval,
                'open_ms':self.open_ms, 'close_ms':self.close_ms,
                **{k:ds(getattr(self,k)) for k in ('o','h','l','c','volume')}}


def validate(bars: list[Bar], *, require_contiguous: bool = True) -> None:
    if not bars:
        raise ValueError('empty dataset')
    first = bars[0]
    interval = INTERVAL_MS[first.interval]
    previous = first.open_ms - interval
    for bar in bars:
        if (bar.market, bar.symbol, bar.interval) != (first.market, first.symbol, first.interval):
            raise ValueError('mixed instrument dataset')
        if bar.open_ms <= previous:
            raise ValueError('duplicate or unordered open timestamp')
        if require_contiguous and bar.open_ms != previous + interval:
            raise ValueError('missing candle interval; stop rather than silently interpolate')
        previous = bar.open_ms


def save_jsonl(path: Path, bars: list[Bar], *, source: str) -> dict:
    validate(bars)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = ''.join(json.dumps(b.as_dict(), sort_keys=True, separators=(',', ':')) + '\n' for b in bars)
    raw = payload.encode()
    path.write_bytes(raw)
    manifest = dict(sha256=hashlib.sha256(raw).hexdigest(), source=source, count=len(bars),
                    market=bars[0].market, symbol=bars[0].symbol,
                    interval=bars[0].interval, first_open_ms=bars[0].open_ms,
                    last_open_ms=bars[-1].open_ms, generated_ms=int(time.time()*1000))
    path.with_suffix(path.suffix + '.manifest.json').write_text(
        json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return manifest


def load_jsonl(path: Path) -> tuple[list[Bar], dict]:
    raw = path.read_bytes()
    manifest = json.loads(path.with_suffix(path.suffix + '.manifest.json').read_text(encoding='utf-8'))
    if manifest['sha256'] != hashlib.sha256(raw).hexdigest():
        raise ValueError('dataset checksum mismatch')
    bars = [Bar.parse(json.loads(line)) for line in raw.splitlines() if line.strip()]
    validate(bars)
    if (len(bars) != manifest['count'] or bars[0].open_ms != manifest['first_open_ms'] or
            bars[-1].open_ms != manifest['last_open_ms'] or
            (bars[0].market, bars[0].symbol, bars[0].interval) !=
            (manifest['market'], manifest['symbol'], manifest['interval'])):
        raise ValueError('manifest metadata mismatch')
    return bars, manifest


async def download(market: str, symbol: str, interval: str, start_ms: int, end_ms: int,
                   *, base_url: str | None = None, client: httpx.AsyncClient | None = None) -> list[Bar]:
    if market not in BASES or interval not in INTERVAL_MS or not symbol.endswith('USDT'):
        raise ValueError('unsupported download instrument')
    if start_ms < 0 or end_ms <= start_ms or end_ms > int(time.time()*1000) - INTERVAL_MS[interval]:
        raise ValueError('request closed historical period only')
    step = INTERVAL_MS[interval]
    if start_ms % step or end_ms % step:
        raise ValueError('start/end must align with candle open boundaries')
    if end_ms - start_ms > 366 * 86400000:
        raise ValueError('download limited to 366 days per job')
    out: list[Bar] = []
    owned = client is None
    session = client or httpx.AsyncClient(timeout=20.0)
    try:
        cursor = start_ms
        while cursor < end_ms:
            page_end = min(end_ms - 1, cursor + 999 * step)
            r = await session.get((base_url or BASES[market]) + PATHS[market],
                                  params={'symbol':symbol,'interval':interval,
                                          'startTime':cursor,'endTime':page_end,'limit':1000})
            if r.status_code in (418,429):
                raise RuntimeError(f'Binance rate limiting: {r.status_code}; retry later')
            r.raise_for_status()
            rows = r.json()
            if not isinstance(rows, list) or not rows:
                raise ValueError(f'missing Binance candles starting {cursor}')
            for row in rows:
                if not isinstance(row, list) or len(row) < 6:
                    raise ValueError('unexpected Binance candle structure')
                open_ms = int(row[0])
                if open_ms >= end_ms:
                    continue
                out.append(Bar.parse(dict(market=market,symbol=symbol,interval=interval,
                    open_ms=open_ms,close_ms=int(row[6]) if len(row)>6 else open_ms+step-1,
                    o=row[1],h=row[2],l=row[3],c=row[4],volume=row[5])))
            new_cursor = out[-1].open_ms + step
            if new_cursor <= cursor:
                raise ValueError('non-progressing historical download')
            cursor = new_cursor
            await asyncio.sleep(0.12)
    finally:
        if owned:
            await session.aclose()
    validate(out)
    if out[0].open_ms != start_ms or out[-1].open_ms != end_ms-step:
        raise ValueError('missing beginning/end historical candles')
    return out


async def import_to_db(path: Path, database_url: str) -> int:
    import psycopg
    from psycopg.types.json import Jsonb
    bars, manifest = load_jsonl(path)
    async with await psycopg.AsyncConnection.connect(database_url) as conn:
        async with conn.transaction():
            await conn.execute('''INSERT INTO historical_manifests
                (sha256,market,symbol,interval,bar_count,first_open_ms,last_open_ms,details)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (manifest['sha256'],manifest['market'],manifest['symbol'],manifest['interval'],
                 manifest['count'],manifest['first_open_ms'],manifest['last_open_ms'],Jsonb(manifest)))
            for b in bars:
                await conn.execute('''INSERT INTO historical_bars
                    (market,symbol,interval,open_ms,close_ms,o,h,l,c,volume,source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (market,symbol,interval,open_ms) DO UPDATE SET
                        close_ms=EXCLUDED.close_ms,o=EXCLUDED.o,h=EXCLUDED.h,
                        l=EXCLUDED.l,c=EXCLUDED.c,volume=EXCLUDED.volume,source=EXCLUDED.source''',
                    (b.market,b.symbol,b.interval,b.open_ms,b.close_ms,
                     b.o,b.h,b.l,b.c,b.volume,manifest['sha256']))
    return len(bars)


def main() -> None:
    p=argparse.ArgumentParser(description='P09 deterministic Binance historical dataset pipeline')
    subs=p.add_subparsers(dest='command', required=True)
    d=subs.add_parser('download')
    d.add_argument('--market',choices=list(BASES),required=True)
    d.add_argument('--symbol',required=True)
    d.add_argument('--interval',choices=list(INTERVAL_MS),default='1m')
    d.add_argument('--start-ms',type=int,required=True)
    d.add_argument('--end-ms',type=int,required=True)
    d.add_argument('--output',type=Path,required=True)
    l=subs.add_parser('load')
    l.add_argument('--file',type=Path,required=True)
    args=p.parse_args()
    if args.command=='download':
        bars=asyncio.run(download(args.market,args.symbol.upper(),args.interval,args.start_ms,args.end_ms))
        print(json.dumps(save_jsonl(args.output,bars,source='binance_rest'),indent=2))
    else:
        import os
        print(f'Imported {asyncio.run(import_to_db(args.file,os.environ["DATABASE_URL"]))} candles')


if __name__=='__main__':
    main()
