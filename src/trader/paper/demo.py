"""Offline reproducible sample data and spot/futures paper-backtest walkthrough.

Explicitly synthetic; does not download, predict, or claim Binance performance.
"""
from __future__ import annotations

import argparse
import json
import math
from decimal import Decimal
from pathlib import Path
from trader.paper.domain import ds
from trader.paper.history import Bar, save_jsonl, load_jsonl
from trader.paper.backtest import simulate


def synthetic(n:int=360,market:str='spot') -> list[Bar]:
    if n<33 or n>10000:
        raise ValueError('size must be 33..10,000')
    start=1735689600000  # 2025-01-01T00:00:00Z, deliberately a past period
    out=[]
    for i in range(n):
        base=Decimal(str(round(100+5*math.sin(i/11)+i*0.008,6)))
        previous=Decimal(str(round(100+5*math.sin((i-1)/11)+(i-1)*0.008,6)))
        opening=previous
        close=base
        out.append(Bar.parse(dict(market=market,symbol='BTCUSDT',interval='1m',
             open_ms=start+i*60000,close_ms=start+i*60000+59999,
             o=ds(opening),h=ds(max(opening,close)+Decimal('.4')),
             l=ds(min(opening,close)-Decimal('.4')),c=ds(close),volume='500')))
    return out


def main():
    parser=argparse.ArgumentParser(description='Run synthetic, offline, virtual-only P09-P14 demo')
    parser.add_argument('--output',type=Path,default=Path('data/examples/synthetic_btcusdt_1m.jsonl'))
    args=parser.parse_args()
    bars=synthetic()
    manifest=save_jsonl(args.output,bars,source='SYNTHETIC_GENERATED_EXAMPLE_NOT_BINANCE')
    verified,_=load_jsonl(args.output)
    result=simulate(verified,fast=10,slow=30)
    summary={k:result[k] for k in ('bar_count','start_equity','end_equity',
             'total_return_fraction','max_drawdown_fraction','fee_total','fills')}
    print(json.dumps({'dataset':str(args.output),'sha256':manifest['sha256'],
                      'backtest_summary':summary,'warning':'synthetic data; not live return evidence'},indent=2))


if __name__=='__main__':
    main()
