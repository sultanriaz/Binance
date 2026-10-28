"""Explicit requests + independent second-admin TOTP approval.

The model registry can only be promoted to SHADOW (never live trading).
"""
from __future__ import annotations
from typing import Annotated, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from trader.api import authorized
from trader.exchange.contracts import ExchangeOrder
from trader.governance.core import ApprovalError
from trader.governance.repository import GovernanceRepo
from trader.security import verify_totp
from trader.operations.security import enforce_mfa_replay

router = APIRouter(prefix='/v1/governance', tags=['independent approvals'])


def admin(claims: Annotated[dict, Depends(authorized)]):
    if claims.get('role') != 'ADMIN':
        raise HTTPException(403,'ADMIN required')
    return claims


class RequestSandboxOrder(BaseModel):
    order: ExchangeOrder


class Review(BaseModel):
    decision: Literal['APPROVED','REJECTED']
    totp: str = Field(pattern='^[0-9]{6}$')
    reason: str = Field(default='',max_length=500)


@router.post('/sandbox-orders', status_code=201)
async def request_order(body:RequestSandboxOrder,request:Request,
                        claims:Annotated[dict,Depends(admin)]):
    try:
        item=await GovernanceRepo(request.app.state.db).create(
            'SANDBOX_ORDER',body.order.model_dump(exclude_none=True),int(claims['sub']))
    except ApprovalError as exc:
        raise HTTPException(422,str(exc)) from None
    return {'request':item,'note':'Independent administrator must approve before any testnet enqueue'}


@router.get('/requests')
async def list_requests(request:Request,claims:Annotated[dict,Depends(admin)]):
    return {'requests':await GovernanceRepo(request.app.state.db).list()}


@router.post('/requests/{request_id}/review')
async def review(request_id:UUID,body:Review,request:Request,
                 claims:Annotated[dict,Depends(admin)]):
    async with request.app.state.db.pool.connection() as conn:
        row = await (await conn.execute('SELECT totp_secret,enabled FROM users WHERE id=%s',
                                      (int(claims['sub']),))).fetchone()
    if not row or not row[1] or not row[0] or not verify_totp(row[0],body.totp):
        raise HTTPException(403,'Valid MFA code required for approval')
    await enforce_mfa_replay(request.app.state.redis, str(claims['sub']), body.totp)
    try:
        return await GovernanceRepo(request.app.state.db).review(
            str(request_id),int(claims['sub']),body.decision,body.reason)
    except ApprovalError as exc:
        raise HTTPException(409,str(exc)) from None


@router.post('/requests/{request_id}/activate-shadow')
async def activate(request_id:UUID,request:Request,claims:Annotated[dict,Depends(admin)]):
    try:
        model_id=await GovernanceRepo(request.app.state.db).promote_shadow(
            str(request_id),int(claims['sub']))
    except ApprovalError as exc:
        raise HTTPException(409,str(exc)) from None
    return {'model_id':model_id,'status':'SHADOW',
            'note':'Research-only observation; cannot place paper, testnet, or live orders'}
