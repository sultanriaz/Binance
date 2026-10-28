"""Authenticated research control plane. No model-generated orders."""
from __future__ import annotations
from typing import Annotated, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from trader.api import authorized
from trader.governance.repository import GovernanceRepo
from trader.intelligence.repository import ResearchRepo

router=APIRouter(prefix='/v1/research',tags=['quantitative research / shadow only'])


def admin(claims: Annotated[dict,Depends(authorized)]):
    if claims.get('role')!='ADMIN':
        raise HTTPException(403,'ADMIN required')
    return claims


class ResearchJobRequest(BaseModel):
    market: Literal['spot','futures'] = 'spot'
    symbol: str = Field(pattern='^[A-Z0-9]{5,20}$',default='BTCUSDT')
    interval: Literal['1m','5m','15m','1h','4h','1d'] = '1m'
    bars: int = Field(default=500,ge=350,le=5000)
    horizon: Literal[1,2,3,5] = 1
    friction_bps: float = Field(default=15,ge=0,le=100)


@router.post('/jobs',status_code=202)
async def submit_job(body:ResearchJobRequest,request:Request,
                     claims:Annotated[dict,Depends(admin)]):
    repo=ResearchRepo(request.app.state.db)
    jobs=await repo.jobs()
    if sum(x['state'] in ('QUEUED','RUNNING') for x in jobs) >= 5:
        raise HTTPException(429,'Research job queue full; do not overload shared database')
    job_id=await repo.request(claims['sub'],body.market,body.symbol,body.interval,
                              body.bars,body.horizon,body.friction_bps)
    await request.app.state.db.audit(str(claims['sub']),'research.job','queued',{'id':job_id})
    return {'id':job_id,'status':'QUEUED','exchange_execution':False}


@router.get('/jobs')
async def jobs(request:Request,claims:Annotated[dict,Depends(admin)]):
    return {'jobs':await ResearchRepo(request.app.state.db).jobs()}


@router.get('/models')
async def models(request:Request,claims:Annotated[dict,Depends(admin)]):
    return {'models':await ResearchRepo(request.app.state.db).models()}


@router.get('/models/{model_id}/shadow')
async def shadow(model_id:UUID,request:Request,claims:Annotated[dict,Depends(admin)]):
    repo=ResearchRepo(request.app.state.db)
    model=await repo.get(str(model_id))
    if not model:
        raise HTTPException(404,'Model not found')
    return {'model_id':str(model_id),'status':model['status'],
            'observations':await repo.shadow(str(model_id)),
            'notice':'Research only; no order execution or strategy auto-activation'}


@router.post('/models/{model_id}/request-shadow',status_code=201)
async def request_shadow(model_id:UUID,request:Request,
                         claims:Annotated[dict,Depends(admin)]):
    model=await ResearchRepo(request.app.state.db).get(str(model_id))
    if not model:
        raise HTTPException(404,'Model not found')
    if model['status']!='CANDIDATE' or not model['quality_pass']:
        raise HTTPException(409,'Candidate failed research gate or has been promoted')
    approval=await GovernanceRepo(request.app.state.db).create(
        'SHADOW_MODEL_PROMOTION',{'model_id':str(model_id)},int(claims['sub']),60)
    return {'request':approval,'note':'Requires a distinct administrator with MFA'}

class CampaignRequest(ResearchJobRequest):
    min_new_bars: int = Field(default=250,ge=50,le=5000)


@router.post('/campaigns',status_code=201)
async def create_campaign(body:CampaignRequest,request:Request,
                          claims:Annotated[dict,Depends(admin)]):
    try:
        id_=await ResearchRepo(request.app.state.db).create_campaign(
            int(claims['sub']),body.market,body.symbol,body.interval,body.bars,
            body.horizon,body.friction_bps,body.min_new_bars)
    except Exception as exc:
        if exc.__class__.__name__=='UniqueViolation':
            raise HTTPException(409,'Campaign for this market/symbol/interval already exists') from None
        raise
    return {'id':id_,'enabled':False,'note':'Disabled initially; activation explicitly required'}


@router.get('/campaigns')
async def campaigns(request:Request,claims:Annotated[dict,Depends(admin)]):
    return {'campaigns':await ResearchRepo(request.app.state.db).campaigns()}


class CampaignSwitch(BaseModel):
    enabled: bool


@router.post('/campaigns/{campaign_id}/enabled')
async def switch_campaign(campaign_id:UUID,body:CampaignSwitch,request:Request,
                          claims:Annotated[dict,Depends(admin)]):
    if not await ResearchRepo(request.app.state.db).set_campaign(str(campaign_id),body.enabled):
        raise HTTPException(404,'Campaign not found')
    await request.app.state.db.audit(str(claims['sub']),'research.campaign',
                                    'enabled' if body.enabled else 'disabled',
                                    {'campaign_id':str(campaign_id)})
    return {'id':str(campaign_id),'enabled':body.enabled,
            'note':'Only automatically queues candidate research; never self-promotes or trades'}


@router.post('/models/{model_id}/retire-shadow')
async def retire(model_id:UUID,request:Request,claims:Annotated[dict,Depends(admin)]):
    if not await ResearchRepo(request.app.state.db).retire_shadow(str(model_id)):
        raise HTTPException(409,'Model not currently shadow-deployed')
    await request.app.state.db.audit(str(claims['sub']),'research.model.retire','accepted',
                                    {'model_id':str(model_id)})
    return {'model_id':str(model_id),'status':'RETIRED'}

@router.get('/models/{model_id}/forecast')
async def forecast(model_id:UUID,request:Request,claims:Annotated[dict,Depends(admin)]):
    """Read-only research inference on newest imported closed bar, no trading."""
    import time
    from trader.intelligence.features import FEATURE_NAMES, make_features, regime
    from trader.intelligence.model import LinearProbabilityModel
    from trader.paper.repository import PaperRepository

    repo=ResearchRepo(request.app.state.db)
    candidate=await repo.get(str(model_id))
    if not candidate or candidate['status']!='SHADOW':
        raise HTTPException(409,'Model must be approved for SHADOW observation')
    bars=await PaperRepository(request.app.state.db).bars(
        candidate['market'],candidate['symbol'],candidate['interval'],150)
    now=int(time.time()*1000)
    bars=[b for b in bars if b.close_ms<now]
    if len(bars)<25 or bars[-1].open_ms<=candidate['train_data_last_ms']:
        raise HTTPException(409,'No unseen closed data since training')
    try:
        row=make_features(bars)[-1]
        model=LinearProbabilityModel.from_artifact(candidate['artifact'])
        p=model.probability(row)
    except ValueError:
        raise HTTPException(409,'Corrupt model or invalid historical bars') from None
    contributions=sorted(((name,weight*(v-mean)/scale) for name,weight,v,mean,scale
               in zip(FEATURE_NAMES,model.weights,row.values,model.means,model.scales)),
               key=lambda x:abs(x[1]),reverse=True)[:3]
    return {'model_id':str(model_id),'market':candidate['market'],
            'symbol':candidate['symbol'],'bar_open_ms':row.open_ms,
            'p_positive_after_cost':round(p,6),
            'signal':'RESEARCH_LONG' if p>=model.threshold else 'RESEARCH_FLAT',
            'decision_threshold':model.threshold,'regime':regime(row),
            'top_linear_contributions':[{'feature':k,'signed_logit_contribution':round(v,6)}
                                        for k,v in contributions],
            'stale':now-bars[-1].close_ms>180000,
            'can_trade':False,'notice':'Research probability, not a real market forecast guarantee'}


@router.get('/models/{model_id}/performance')
async def shadow_performance(model_id:UUID,request:Request,
                             claims:Annotated[dict,Depends(admin)]):
    repo=ResearchRepo(request.app.state.db)
    candidate=await repo.get(str(model_id))
    if not candidate:
        raise HTTPException(404,'Unknown model')
    observations=await repo.shadow(str(model_id))
    resolved=[x for x in observations if x['resolved_net_return'] is not None]
    if not resolved:
        return {'model_id':str(model_id),'sample_size':0,
                'signal':'INSUFFICIENT_SHADOW_EVIDENCE'}
    brier=sum((x['p_up']-int(x['resolved_net_return']>0))**2
              for x in resolved)/len(resolved)
    caution=len(resolved)<50 or brier>0.30
    return {'model_id':str(model_id),'sample_size':len(resolved),
            'shadow_brier':brier,
            'signal':'REVIEW_MODEL' if caution else 'MONITOR',
            'notice':'Observation sample may be autocorrelated; result does not establish profitability'}
