"""Operator control plane. NEVER holds Binance keys and NEVER performs exchange writes."""
from __future__ import annotations
import os
from typing import Annotated,Literal
from uuid import UUID
from fastapi import APIRouter,Depends,HTTPException,Request
from pydantic import BaseModel,Field,model_validator
from trader.api import authorized
from trader.live.contracts import LiveOrder,AutomationConfig
from trader.live.authorization import LiveAuthorization
from trader.live.repository import LiveRepository,LiveStateError
from trader.governance.core import ApprovalError
from trader.security import verify_totp
from trader.operations.security import enforce_mfa_replay

router=APIRouter(prefix='/v1/live',tags=['controlled live trading'])

async def admin(claims:Annotated[dict,Depends(authorized)]):
    if claims.get('role')!='ADMIN':raise HTTPException(403,'Administrator only')
    if os.getenv('LIVE_CONTROL_ENABLED','false').lower()!='true' or os.getenv('DEPLOYMENT_MODE')!='production':
        raise HTTPException(403,'Live control plane is disabled by deployment policy')
    return claims

async def step_up(request,subject,totp):
    async with request.app.state.db.pool.connection() as conn:
        row=await (await conn.execute('''SELECT totp_secret,enabled,role FROM users WHERE id=%s''',
                                     (int(subject),))).fetchone()
    if not row or not row[1] or row[2]!='ADMIN' or not row[0] or not verify_totp(row[0],totp):
        raise HTTPException(403,'Valid administrator MFA required')
    await enforce_mfa_replay(request.app.state.redis,str(subject),totp)

class Proposal(BaseModel):
    kind: Literal['SPOT_ARM','FUTURES_ARM','SPOT_AUTOMATION','SPOT_ORDER','FUTURES_ORDER']
    order: LiveOrder | None = None
    automation: AutomationConfig | None = None
    @model_validator(mode='after')
    def valid(self):
        if self.kind.endswith('_ORDER'):
            account='spot-live' if self.kind=='SPOT_ORDER' else 'futures-live'
            if self.order is None or self.order.account!=account or self.automation is not None:
                raise ValueError('Order must match the requested live account')
        elif self.kind=='SPOT_AUTOMATION':
            if self.order is not None or self.automation is None:
                raise ValueError('Validated automation configuration required')
        elif self.order is not None or self.automation is not None:
            raise ValueError('Arm proposals have no order/strategy fields')
        return self

    def authorized_payload(self):
        if self.kind=='SPOT_ARM':return {'account':'spot-live'}
        if self.kind=='FUTURES_ARM':return {'account':'futures-live'}
        if self.kind=='SPOT_AUTOMATION':return {'account':'spot-live','config':self.automation.model_dump()}
        o=self.order
        return {'account':o.account,'client_order_id':o.client_order_id,
                'order':o.model_dump(exclude={'account','client_order_id'},exclude_none=True)}

class MFAReview(BaseModel):
    decision: Literal['APPROVED','REJECTED']
    totp: str=Field(pattern=r'^\d{6}$')
    reason: str=Field(default='',max_length=500)

class ExecuteApproval(BaseModel):
    proposal: Proposal
    totp: str=Field(pattern=r'^\d{6}$')

class IncidentReview(BaseModel):
    acknowledgement: Literal['I_INDEPENDENTLY_VERIFIED_EXCHANGE_STATE']
    totp: str=Field(pattern=r'^\d{6}$')

class PanicReview(BaseModel):
    confirmation: Literal['I_CONFIRM_EMERGENCY_FLATTEN_ALL_POSITIONS']
    totp: str=Field(pattern=r'^\d{6}$')


@router.get('/capabilities')
async def capabilities(claims:Annotated[dict,Depends(admin)]):
    return {'environment':'PRODUCTION','credential_location':'DEDICATED_WORKER_ONLY',
            'live_worker_enabled':'NOT_EXPOSED_TO_GATEWAY',
            'requires_independent_review':True,'risk_caps_enforced_in_worker':True,
            'futures_eligible_acknowledged':os.getenv('LIVE_FUTURES_ELIGIBILITY_CONFIRMED','false')=='true'}

@router.get('/accounts')
async def accounts(request:Request,claims:Annotated[dict,Depends(admin)]):
    repo=LiveRepository(request.app.state.db)
    return {'accounts':await repo.accounts(), 'readiness':'INDEPENDENT_GATE_REQUIRED'}

@router.get('/accounts/{code}/orders')
async def orders(code:Literal['spot-live','futures-live'],request:Request,
                 claims:Annotated[dict,Depends(admin)]):
    repo=LiveRepository(request.app.state.db)
    return {'orders':await repo.orders(code),'incidents':await repo.incidents(code)}

@router.get('/approvals')
async def approvals(request:Request,claims:Annotated[dict,Depends(admin)]):
    return {'requests':await LiveAuthorization(request.app.state.db).list()}

@router.post('/approvals',status_code=201)
async def create_proposal(body:Proposal,request:Request,claims:Annotated[dict,Depends(admin)]):
    if body.kind.startswith('FUTURES') and os.getenv('LIVE_FUTURES_ELIGIBILITY_CONFIRMED')!='true':
        raise HTTPException(403,'Futures permission/eligibility not confirmed')
    try:
        return await LiveAuthorization(request.app.state.db).create(body.kind,body.authorized_payload(),
                                                                     int(claims['sub']))
    except ApprovalError as exc:raise HTTPException(409,str(exc)) from None

@router.post('/approvals/{approval_id}/review')
async def review(approval_id:UUID,body:MFAReview,request:Request,
                 claims:Annotated[dict,Depends(admin)]):
    await step_up(request,claims['sub'],body.totp)
    try:
        return await LiveAuthorization(request.app.state.db).review(str(approval_id),
                          int(claims['sub']),body.decision,body.reason)
    except ApprovalError as exc:raise HTTPException(409,str(exc)) from None

@router.post('/approvals/{approval_id}/execute')
async def execute(approval_id:UUID,body:ExecuteApproval,request:Request,
                  claims:Annotated[dict,Depends(admin)]):
    await step_up(request,claims['sub'],body.totp)
    repo=LiveRepository(request.app.state.db)
    p=body.proposal
    if p.kind.startswith('FUTURES') and os.getenv('LIVE_FUTURES_ELIGIBILITY_CONFIRMED')!='true':
        raise HTTPException(403,'Live futures eligibility acknowledgment missing')
    try:
        if p.kind.endswith('_ARM'):
            code='spot-live' if p.kind=='SPOT_ARM' else 'futures-live'
            await repo.arm(code,str(approval_id),int(claims['sub']))
            result={'armed':True,'account':code}
        elif p.kind=='SPOT_AUTOMATION':
            await repo.set_automation(str(approval_id),int(claims['sub']),p.automation.model_dump())
            result={'automation_enabled':True,'account':'spot-live'}
        else:
            o=p.order
            result=await repo.enqueue(o.account,o.client_order_id,
                o.model_dump(exclude={'account','client_order_id'},exclude_none=True),
                str(approval_id),int(claims['sub']))
    except (LiveStateError,ApprovalError) as exc:
        raise HTTPException(409,str(exc)) from None
    await request.app.state.db.audit(str(claims['sub']),'live.authorization.consumed', 'accepted',
                                      {'kind':p.kind,'approval_id':str(approval_id)})
    return {'result':result,'environment':'REAL_MONEY','exchange_write':'WORKER_PENDING'}

@router.get('/accounts/spot-live/fee-buffer')
async def spot_fee_buffer(request:Request, claims:Annotated[dict,Depends(admin)]):
    repo=LiveRepository(request.app.state.db)
    account=await repo.get_account('spot-live')
    if not account or not account.get('snapshot'):
        return {'sufficient_fee_buffer':False,'error':'No recent spot account snapshot available'}
    balances=account['snapshot'].get('account',{}).get('balances',[])
    from trader.live.risk import check_bnb_fee_buffer
    return check_bnb_fee_buffer(balances)

@router.post('/accounts/{code}/halt')
async def halt(code:Literal['spot-live','futures-live'],request:Request,
               claims:Annotated[dict,Depends(admin)]):
    await LiveRepository(request.app.state.db).set_halt(code)
    await request.app.state.db.audit(str(claims['sub']),'live.halt','accepted',{'account':code})
    return {'halted':True,'warning':'New entries disabled. Existing positions and orders remain active.'}

@router.post('/accounts/{code}/panic-flatten')
async def panic_flatten(code:Literal['spot-live','futures-live'],body:PanicReview,request:Request,
                        claims:Annotated[dict,Depends(admin)]):
    await step_up(request,claims['sub'],body.totp)
    repo=LiveRepository(request.app.state.db)
    await repo.set_panic_halt(code)
    await request.app.state.db.audit(str(claims['sub']),'live.panic_flatten','accepted',{'account':code})
    from trader.live.alerts import send_alert
    await send_alert(
        title="EMERGENCY PANIC-FLATTEN ACTIVATED",
        message=f"Operator {claims['sub']} triggered Tier 2 emergency panic flatten for {code}. All queued orders canceled, open exchange orders will be canceled, and positions flattened.",
        level="CRITICAL",
        metadata={'account':code,'operator':str(claims['sub'])}
    )
    return {
        'panic_flatten_initiated':True,
        'account':code,
        'tier':'TIER_2_EMERGENCY_KILL_SWITCH',
        'status':'HALTED',
        'exchange_action':'WORKER_IMMEDIATE_CANCEL_AND_FLATTEN'
    }


@router.post('/accounts/{code}/orders/{intent_id}/cancel',status_code=202)
async def cancel(code:Literal['spot-live','futures-live'],intent_id:UUID,request:Request,
                 claims:Annotated[dict,Depends(admin)]):
    repo=LiveRepository(request.app.state.db)
    if await repo.cancel_queued(code,str(intent_id)):
        return {'status':'CANCELED','exchange_write':False}
    if not await repo.request_cancel(code,str(intent_id)):
        raise HTTPException(409,'Order not cancellable or not found')
    return {'status':'CANCEL_REQUESTED','exchange_write':'WORKER_PENDING'}

@router.post('/accounts/{code}/incidents/{incident_id}/acknowledge')
async def acknowledge(code:Literal['spot-live','futures-live'],incident_id:int,
                      body:IncidentReview,request:Request,claims:Annotated[dict,Depends(admin)]):
    await step_up(request,claims['sub'],body.totp)
    if not await LiveRepository(request.app.state.db).ack(code,incident_id,int(claims['sub'])):
        raise HTTPException(404,'Incident not found')
    return {'acknowledged':True,'warning':'Does not arm, resume automation or resolve unknown trades'}

@router.get('/operations/report')
async def operational_report(request:Request,claims:Annotated[dict,Depends(admin)]):
    from trader.live.reporting import live_report
    return await live_report(request.app.state.db)
