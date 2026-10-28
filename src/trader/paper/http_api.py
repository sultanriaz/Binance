"""Authenticated and admin-only PAPER trading endpoints. No exchange writes."""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from trader.api import authorized
from trader.paper.backtest import simulate
from trader.paper.domain import Quote, dec
from trader.paper.engine import cancel_order, place_order
from trader.paper.repository import PaperRepository, policy_dict
from trader.paper.risk import RiskRejected

router = APIRouter(prefix='/v1/paper', tags=['paper (virtual only)'])


def admin(claims: Annotated[dict, Depends(authorized)]) -> dict:
    if claims.get('role') != 'ADMIN':
        raise HTTPException(403, 'Administrator permission required')
    return claims


def ident(id_: str) -> str:
    try:
        return str(UUID(id_))
    except ValueError:
        raise HTTPException(422,'Invalid account identifier') from None


class CreateAccount(BaseModel):
    name: str = Field(min_length=3,max_length=80)
    initial_usdt: str = Field(default='10000')

    @field_validator('initial_usdt')
    @classmethod
    def cash_limit(cls, value: str) -> str:
        n=dec(value)
        if not Decimal('100') <= n <= Decimal('1000000'):
            raise ValueError('virtual initial balance must be 100..1,000,000 USDT')
        return value


class OrderRequest(BaseModel):
    market: Literal['spot','futures']
    symbol: str = Field(pattern='^[A-Z0-9]{5,20}$')
    side: Literal['BUY','SELL']
    quantity: str
    type: Literal['MARKET','LIMIT','STOP_MARKET','TAKE_PROFIT_MARKET'] = 'MARKET'
    limit_price: str | None = None
    trigger_price: str | None = None
    oco_group: str | None = Field(default=None,max_length=80)
    leverage: int = Field(default=1,ge=1,le=10)
    reduce_only: bool = False
    client_id: str = Field(min_length=8,max_length=100)

    @field_validator('quantity')
    @classmethod
    def qty_limit(cls,value: str) -> str:
        if not Decimal('0') < dec(value) <= Decimal('1000000'):
            raise ValueError('quantity outside supported positive range')
        return value


class BacktestRequest(BaseModel):
    market: Literal['spot','futures']
    symbol: str = Field(pattern='^[A-Z0-9]{5,20}$')
    interval: Literal['1m','5m','15m','1h','4h','1d'] = '1m'
    bars: int = Field(default=500,ge=33,le=5000)
    fast: int = Field(default=10,ge=2,le=100)
    slow: int = Field(default=30,ge=3,le=300)
    allocation: str = '0.10'
    initial_usdt: str = '10000'


class StrategyRequest(BaseModel):
    account_id: str
    market: Literal['spot','futures']
    symbol: str = Field(pattern='^[A-Z0-9]{5,20}$')
    fast: int = Field(default=10,ge=2,le=100)
    slow: int = Field(default=30,ge=3,le=300)
    allocation: str = '0.10'
    enabled: bool = False


async def latest_quote(request: Request, market: str, symbol: str) -> Quote:
    rows=await request.app.state.db.latest(market,symbol)
    b=next((e for e in rows if e['event_type']=='book_ticker'),None)
    if b is None:
        raise HTTPException(503,'No trusted live best-bid/ask observation available')
    p=b['payload']
    try:
        quote=Quote(market,symbol,dec(p['best_bid']),dec(p['best_ask']),
                    dec(p['bid_qty']),dec(p['ask_qty']),int(b['received_ts']))
    except (ValueError,KeyError) as exc:
        raise HTTPException(503,'Invalid latest market-data observation') from exc
    if abs(int(time.time()*1000)-quote.ts_ms)>15000:
        raise HTTPException(503,'Market observation stale; no paper order placed')
    return quote


@router.post('/accounts', status_code=201)
async def create_account(body: CreateAccount, request: Request,
                         claims: Annotated[dict, Depends(admin)]):
    repo=PaperRepository(request.app.state.db)
    a=await repo.create_account(body.name,dec(body.initial_usdt))
    await request.app.state.db.audit(claims['sub'],'paper.account.create','accepted',
                                      {'account_id':a.account_id})
    return {'account':a.to_dict(),'note':'VIRTUAL USDT ONLY; no deposit or exchange orders'}


@router.get('/accounts')
async def accounts(request: Request, claims: Annotated[dict, Depends(admin)]):
    return {'accounts':await PaperRepository(request.app.state.db).list_accounts()}


@router.get('/accounts/{account_id}')
async def account(account_id: str,request: Request,claims: Annotated[dict, Depends(admin)]):
    record=await PaperRepository(request.app.state.db).read_account(ident(account_id))
    if record is None:
        raise HTTPException(404,'Account not found')
    a,p=record
    return {'account':a.to_dict(),'equity':str(a.equity()),'gross_exposure':str(a.gross_exposure()),
            'policy':policy_dict(p)}


@router.post('/accounts/{account_id}/orders')
async def submit_order(account_id: str, body: OrderRequest,request: Request,
                       claims: Annotated[dict, Depends(admin)]):
    if body.type=='LIMIT' and body.limit_price is None:
        raise HTTPException(422,'limit_price is required for LIMIT')
    if body.type!='LIMIT' and body.limit_price is not None:
        raise HTTPException(422,'limit_price only supported for LIMIT')
    if body.type in ('STOP_MARKET','TAKE_PROFIT_MARKET') and body.trigger_price is None:
        raise HTTPException(422,'trigger_price required for conditional order')
    quote=await latest_quote(request,body.market,body.symbol)
    repo=PaperRepository(request.app.state.db)
    try:
        a,order=await repo.mutate(ident(account_id),lambda account,policy,conn:
            place_order(account,quote,policy,side=body.side,quantity=dec(body.quantity),
                        ts_ms=int(time.time()*1000),order_type=body.type,
                        limit_price=dec(body.limit_price) if body.limit_price is not None else None,
                        leverage=body.leverage,reduce_only=body.reduce_only,client_id=body.client_id,
                        trigger_price=dec(body.trigger_price) if body.trigger_price is not None else None,
                        oco_group=body.oco_group))
    except KeyError:
        raise HTTPException(404,'Account not found') from None
    except RiskRejected as exc:
        await request.app.state.db.audit(claims['sub'],'paper.order','risk_rejected',
                                          {'account_id':account_id,'reason':exc.reason})
        raise HTTPException(409,{'paper_order_rejected':exc.reason}) from None
    return {'order':order,'equity':str(a.equity()),'virtual_only':True}


@router.post('/accounts/{account_id}/orders/{order_id}/cancel')
async def cancel(account_id:str,order_id:str,request:Request,
                 claims:Annotated[dict,Depends(admin)]):
    try:
        _,order=await PaperRepository(request.app.state.db).mutate(
            ident(account_id),lambda a,p,c:cancel_order(a,order_id,int(time.time()*1000)))
    except KeyError:
        raise HTTPException(404,'Account not found') from None
    except ValueError:
        raise HTTPException(404,'Order not found') from None
    return {'order':order}


@router.post('/accounts/{account_id}/pause')
async def pause(account_id:str,request:Request,claims:Annotated[dict,Depends(admin)]):
    def change(a,p,c):
        a.paused=True
        a.audit('TRADING_PAUSED',int(time.time()*1000),actor=str(claims['sub']))
        return a.paused
    try:
        a,_=await PaperRepository(request.app.state.db).mutate(ident(account_id),change)
    except KeyError:
        raise HTTPException(404,'Account not found') from None
    return {'paused':a.paused,'note':'Existing positions are not automatically liquidated'}


@router.post('/accounts/{account_id}/resume')
async def resume(account_id:str,request:Request,claims:Annotated[dict,Depends(admin)]):
    def change(a,p,c):
        if a.equity() <= 0 or (a.peak_equity-a.equity())/a.peak_equity>=p.max_total_drawdown_fraction:
            raise RiskRejected('RISK_REVIEW_REQUIRED')
        a.paused=False
        a.audit('TRADING_RESUMED',int(time.time()*1000),actor=str(claims['sub']))
    try:
        a,_=await PaperRepository(request.app.state.db).mutate(ident(account_id),change)
    except KeyError:
        raise HTTPException(404,'Account not found') from None
    except RiskRejected as exc:
        raise HTTPException(409,exc.reason) from None
    return {'paused':a.paused}


@router.post('/backtests')
async def backtest(body:BacktestRequest,request:Request,claims:Annotated[dict,Depends(admin)]):
    if body.fast >= body.slow or body.bars < body.slow+2:
        raise HTTPException(422,'bar count must exceed slow window + 1')
    if not Decimal('0')<dec(body.allocation)<=Decimal('.2'):
        raise HTTPException(422,'allocation must be between 0 and 0.20')
    if not Decimal('100')<=dec(body.initial_usdt)<=Decimal('1000000'):
        raise HTTPException(422,'initial balance must be 100..1,000,000')
    repo=PaperRepository(request.app.state.db)
    bars=await repo.bars(body.market,body.symbol,body.interval,body.bars)
    if len(bars)<body.bars:
        raise HTTPException(422,f'Only {len(bars)} imported historical bars; request fewer or ingest more')
    try:
        result=await asyncio.to_thread(simulate,bars,starting_cash=dec(body.initial_usdt),
                fast=body.fast,slow=body.slow,allocation=dec(body.allocation))
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    settings=body.model_dump()
    run_id=await repo.store_backtest(account_id=None,market=body.market,symbol=body.symbol,
                                     interval=body.interval,settings=settings,result=result)
    summary={k:v for k,v in result.items() if k not in ('final_state','equity_curve')}
    return {'run_id':run_id,'summary':summary,'curve_points':len(result['equity_curve'])}


@router.get('/backtests')
async def backtest_list(request:Request,claims:Annotated[dict,Depends(admin)]):
    return {'runs':await PaperRepository(request.app.state.db).list_backtests()}


@router.post('/strategies',status_code=201)
async def strategy_create(body:StrategyRequest,request:Request,claims:Annotated[dict,Depends(admin)]):
    from uuid import uuid4
    if body.fast>=body.slow or not Decimal('0')<dec(body.allocation)<=Decimal('.2'):
        raise HTTPException(422,'Invalid strategy windows or allocation')
    account_id=ident(body.account_id)
    if await PaperRepository(request.app.state.db).read_account(account_id) is None:
        raise HTTPException(404,'Paper account not found')
    strategy_id=str(uuid4())
    try:
        async with request.app.state.db.pool.connection() as conn:
            await conn.execute('''INSERT INTO paper_strategies
                (id,account_id,market,symbol,fast_window,slow_window,allocation_fraction,enabled)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                (strategy_id,account_id,body.market,body.symbol,body.fast,body.slow,
                 dec(body.allocation),body.enabled))
    except Exception as exc:
        # Database integrity errors are surfaced as conflicts, not leaked SQL diagnostics.
        if exc.__class__.__name__=='UniqueViolation':
            raise HTTPException(409,'Strategy already exists for this account/instrument') from None
        raise
    return {'id':strategy_id,'enabled':body.enabled,'virtual_only':True}


@router.get('/strategies')
async def strategies(request:Request,claims:Annotated[dict,Depends(admin)]):
    async with request.app.state.db.pool.connection() as conn:
        rows=await (await conn.execute('''SELECT id,account_id,market,symbol,
            fast_window,slow_window,allocation_fraction,enabled,last_closed_bar_ms
            FROM paper_strategies ORDER BY created_at DESC LIMIT 100''')).fetchall()
    return {'strategies':[dict(id=str(x[0]),account_id=str(x[1]),market=x[2],symbol=x[3],
               fast=x[4],slow=x[5],allocation=str(x[6]),enabled=x[7],last_closed_bar_ms=x[8]) for x in rows]}


@router.post('/strategies/{strategy_id}/enabled')
async def strategy_toggle(strategy_id:str,enabled:bool,request:Request,
                          claims:Annotated[dict,Depends(admin)]):
    async with request.app.state.db.pool.connection() as conn:
        row=await (await conn.execute('''UPDATE paper_strategies SET enabled=%s
            WHERE id=%s RETURNING id''',(enabled,ident(strategy_id)))).fetchone()
    if row is None:
        raise HTTPException(404,'Strategy not found')
    return {'id':str(row[0]),'enabled':enabled}
