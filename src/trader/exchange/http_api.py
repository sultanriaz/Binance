"""Admin-only sandbox control plane. Only queues intents; NO direct exchange HTTP writes."""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID
from trader.governance.core import ApprovalError

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from trader.api import authorized
from trader.exchange.repository import Conflict, ExchangeRepo
from trader.exchange.contracts import ExchangeOrder
from trader.exchange.settings import ExchangeSettings, HOSTS

router = APIRouter(prefix='/v1/testnet',tags=['testnet / futures demo'])


def admin(claims: Annotated[dict,Depends(authorized)]):
    if claims.get('role') != 'ADMIN':
        raise HTTPException(403,'ADMIN required')
    return claims




@router.get('/capabilities')
async def capabilities(claims: Annotated[dict,Depends(admin)]):
    s=ExchangeSettings.load()
    return {'environment':'SANDBOX_ONLY','live_trading_enabled':False,'enabled':s.active and s.confirmed,
            'accounts':{x:bool(s.active and s.confirmed) for x in HOSTS},'symbols':s.symbols,
            'max_order_usdt':s.max_order_usdt,'max_daily_orders':s.max_daily_orders,
            'max_leverage':s.max_leverage}


@router.get('/accounts')
async def accounts(request:Request, claims:Annotated[dict,Depends(admin)]):
    return {'accounts':await ExchangeRepo(request.app.state.db).accounts()}


@router.get('/accounts/{code}/orders')
async def orders(code:Literal['spot-testnet','futures-demo'],request:Request,
                 claims:Annotated[dict,Depends(admin)]):
    repo=ExchangeRepo(request.app.state.db)
    return {'orders':await repo.intents(code),'incidents':await repo.open_incidents(code)}


@router.post('/orders',status_code=202)
async def place(body:ExchangeOrder,request:Request,claims:Annotated[dict,Depends(admin)],
                approval_id: UUID | None = None):
    if approval_id is None:
        raise HTTPException(403,'Approved independent dual-control request required')
    s=ExchangeSettings.load()
    if not s.active or not s.confirmed:
        raise HTTPException(403,'Explicit sandbox enablement required')
    if body.symbol not in s.symbols:
        raise HTTPException(422,'Symbol not on testnet allowlist')
    repo=ExchangeRepo(request.app.state.db)
    a=await repo.get_account(body.account)
    if not a or not a['operator_enabled'] or a['state']!='READY':
        raise HTTPException(409,'Testnet account not reconciled and operator-armed')
    payload=body.model_dump(exclude={'account','client_order_id'},exclude_none=True)
    try:
        result=await repo.enqueue(body.account,body.client_order_id,payload,str(claims['sub']),str(approval_id))
    except (Conflict, ApprovalError) as exc:
        raise HTTPException(409,str(exc)) from None
    await request.app.state.db.audit(str(claims['sub']),'testnet.order.enqueue','accepted',
                                     {'account':body.account,'client_id':body.client_order_id})
    return {'order':result,'environment':'SANDBOX_ONLY','note':'Queued for sandbox worker; not yet executed'}


@router.post('/accounts/{code}/orders/{order_id}/cancel',status_code=202)
async def cancel(code:Literal['spot-testnet','futures-demo'],order_id:UUID,request:Request,
                 claims:Annotated[dict,Depends(admin)]):
    repo=ExchangeRepo(request.app.state.db)
    if await repo.cancel_queued(code,str(order_id)):
        return {'status':'CANCELED','exchange_write':False}
    if not await repo.request_cancel(code,str(order_id)):
        raise HTTPException(409,'Order not cancellable or not found')
    await request.app.state.db.audit(str(claims['sub']),'testnet.order.cancel','requested',
                                      {'account':code,'intent_id':str(order_id)})
    return {'status':'CANCEL_REQUESTED','exchange_write':'WORKER_PENDING'}


@router.post('/accounts/{code}/arm')
async def arm(code:Literal['spot-testnet','futures-demo'],request:Request,
              claims:Annotated[dict,Depends(admin)]):
    s=ExchangeSettings.load()
    if not s.active or not s.confirmed:
        raise HTTPException(403,'Sandbox flag or acknowledgement missing')
    if not await ExchangeRepo(request.app.state.db).set_operator_enabled(code, True):
        raise HTTPException(409,'Account requires complete reconciliation and acknowledged incident review')
    await request.app.state.db.audit(str(claims['sub']),'testnet.arm','accepted',{'account':code})
    return {'armed':True,'environment':'SANDBOX_ONLY'}


@router.post('/accounts/{code}/halt')
async def halt(code:Literal['spot-testnet','futures-demo'],request:Request,
               claims:Annotated[dict,Depends(admin)]):
    await ExchangeRepo(request.app.state.db).set_operator_enabled(code,False)
    await request.app.state.db.audit(str(claims['sub']),'testnet.halt','accepted',{'account':code})
    return {'armed':False,'note':'No new exchange entries. Existing positions/orders remain open.'}


class IncidentAck(BaseModel):
    acknowledgement: Literal['I_REVIEWED_EXCHANGE_ACCOUNT_STATE']


@router.post('/accounts/{code}/incidents/{incident_id}/acknowledge')
async def ack(code:Literal['spot-testnet','futures-demo'],incident_id:int,body:IncidentAck,
              request:Request,claims:Annotated[dict,Depends(admin)]):
    if not await ExchangeRepo(request.app.state.db).acknowledge(code,incident_id):
        raise HTTPException(404,'Open incident not found')
    await request.app.state.db.audit(str(claims['sub']),'testnet.incident.acknowledged','accepted',
                                      {'account':code,'incident_id':incident_id})
    return {'acknowledged':True,'note':'Acknowledgement does NOT resume execution; reconciliation must succeed first'}
