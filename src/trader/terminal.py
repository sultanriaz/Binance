"""Batched read-only terminal endpoints, sourced from durable imported candles.

No direct exchange writes or browser-supplied SQL; supported interval allowlist.
"""
from __future__ import annotations
import time
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Request, Query
from trader.api import authorized
from trader.paper.repository import PaperRepository
from trader.intelligence.features import make_features, regime

router = APIRouter(prefix='/v1/terminal',tags=['trading terminal read-only'])


@router.get('/candles')
async def candles(request:Request,claims:Annotated[dict,Depends(authorized)],
                  market:Literal['spot','futures']='spot',
                  symbol:str=Query(default='BTCUSDT',pattern='^[A-Z0-9]{5,20}$'),
                  interval:Literal['1m','5m','15m','1h','4h','1d']='1m',
                  limit:int=Query(default=250,ge=30,le=1000)):
    bars=await PaperRepository(request.app.state.db).bars(market,symbol,interval,limit)
    now=int(time.time()*1000)
    closed=[b for b in bars if b.close_ms<now]
    response={'market':market,'symbol':symbol,'interval':interval,
              'bars':[{'t':int(b.open_ms/1000),'open':float(b.o),'high':float(b.h),
                       'low':float(b.l),'close':float(b.c),'volume':float(b.volume)} for b in closed],
              'source':'imported Binance historical_bars, closed only',
              'fresh':bool(closed and now-closed[-1].close_ms < 3 * 60 * 1000),
              'notice':'Research candles; not an executable quote'}
    if len(closed)>=23:
        response['regime']=regime(make_features(closed)[-1])
    return response
