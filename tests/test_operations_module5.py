"""P25–P28 offline safety, reproducibility and failure-injection acceptance tests."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from trader.operations.readiness import GatePolicy, evaluate, evidence_digest
from trader.operations.security import enforce_mfa_replay, validate_deployment_environment
from trader.exchange.engine import exchange_status
from trader.exchange.repository import digest
from trader.operations.repository import OperationsRepository


def sound_evidence():
    return {
        'database_healthy': True,
        'collector_streams':[{'market':'spot','connected':True,'age_seconds':2},
                             {'market':'futures','connected':True,'age_seconds':3}],
        'worker_heartbeats':[{'service':s,'status':'HEALTHY','age_seconds':1}
            for s in ('collector','paper-worker','research-worker','outbox','operations-worker')],
        'unresolved_market_gaps':0,'open_exchange_incidents':0,
        'unknown_exchange_orders':0,'open_operations_incidents':0,
        'backup_age_seconds':60,
        'shadow_models':[{'id':'model1','status':'SHADOW','resolved_count':150,
                          'span_days':22,'brier':0.17,'age_seconds':60}]
    }


def test_passed_evidence_still_cannot_authorize_trade():
    result=evaluate(sound_evidence())
    assert result['verdict']=='REVIEW_REQUIRED'
    assert result['live_trading_authorized'] is False
    assert result['human_approval_required'] is True
    assert result['reasons']==[]


@pytest.mark.parametrize('key,value,expected',[
    ('database_healthy',False,'DATABASE_NOT_VERIFIED'),
    ('collector_streams',[],'NO_MARKET_STREAM_EVIDENCE'),
    ('unresolved_market_gaps',1,'MARKET_DATA_GAPS'),
    ('open_exchange_incidents',1,'EXCHANGE_INCIDENTS'),
    ('unknown_exchange_orders',1,'UNKNOWN_ORDER_OUTCOMES'),
    ('open_operations_incidents',1,'OPERATIONS_INCIDENTS'),
    ('backup_age_seconds',90000,'BACKUP_NOT_RECENTLY_VERIFIED'),
    ('shadow_models',[],'NO_APPROVED_SHADOW_MODEL'),
])
def test_injected_critical_failure_blocks_readiness(key,value,expected):
    evidence=sound_evidence()
    evidence[key]=value
    out=evaluate(evidence)
    assert out['verdict']=='NO_GO'
    assert expected in out['reasons']
    assert out['live_trading_authorized'] is False


def test_stale_worker_market_and_model_block():
    x=sound_evidence()
    x['worker_heartbeats'][0]['age_seconds']=150
    x['collector_streams'][1]['age_seconds']=600
    x['shadow_models'][0]['age_seconds']=400
    reasons=evaluate(x)['reasons']
    assert 'WORKER_UNHEALTHY_COLLECTOR' in reasons
    assert 'STALE_MARKET_STREAM' in reasons
    assert 'STALE_SHADOW_OBSERVATIONS' in reasons


def test_low_sample_bad_calibration_and_short_duration_block():
    x=sound_evidence()
    x['shadow_models'][0].update(resolved_count=20,span_days=2,brier=.4)
    reasons=evaluate(x)['reasons']
    assert {'INSUFFICIENT_RESOLVED_SHADOW_TRADES','INSUFFICIENT_SHADOW_DURATION',
            'SHADOW_CALIBRATION_NOT_ACCEPTABLE'} <= set(reasons)


def test_missing_evidence_is_never_accepted():
    result=evaluate({})
    assert result['verdict']=='NO_GO'
    assert any(x.startswith('MISSING_') for x in result['reasons'])


def test_invalid_policy_rejected():
    with pytest.raises(ValueError):
        GatePolicy(min_days=1)
    with pytest.raises(ValueError):
        GatePolicy(max_brier=.90)


def test_evidence_hash_is_stable_and_tamper_evident():
    x=evaluate(sound_evidence())
    h=evidence_digest(x,None)
    assert h==evidence_digest(deepcopy(x),None)
    y=deepcopy(x)
    y['evidence']['open_exchange_incidents']=1
    assert evidence_digest(y,None)!=h
    assert evidence_digest(x,h)!=h


@pytest.mark.parametrize('deployment,env',[
    ('live',{'ENABLE_LIVE_TRADING':'true'}),
    ('other',{'DEPLOYMENT_MODE':'production'}),
    ('staging_no_role',{'DEPLOYMENT_MODE':'staging','JWT_SECRET':'x'*50,'TRUSTED_TLS_PROXY':'true'}),
    ('staging_no_tls',{'DEPLOYMENT_MODE':'staging','JWT_SECRET':'x'*50,'STAGING_DB_ROLE':'trader_gateway'}),
])
def test_unsafe_deployment_refused(deployment,env):
    with pytest.raises(RuntimeError):
        validate_deployment_environment(env)


def test_safe_staging_requires_segregated_role_and_tls():
    validate_deployment_environment({'DEPLOYMENT_MODE':'staging','JWT_SECRET':'x'*50,
        'STAGING_DB_ROLE':'trader_gateway','TRUSTED_TLS_PROXY':'true',
        'ENABLE_LIVE_TRADING':'false','ENABLE_TESTNET_EXECUTION':'false'})


class FakeCache:
    def __init__(self): self.used=set()
    async def set(self,key,value,nx,ex):
        if key in self.used: return None
        self.used.add(key)
        return True


@pytest.mark.asyncio
async def test_mfa_code_reuse_rejected():
    cache=FakeCache()
    await enforce_mfa_replay(cache,'admin-1','123456')
    with pytest.raises(HTTPException) as exc:
        await enforce_mfa_replay(cache,'admin-1','123456')
    assert exc.value.status_code==409
    await enforce_mfa_replay(cache,'admin-2','123456')


@pytest.mark.asyncio
async def test_mfa_backend_outage_fails_closed():
    class Broken:
        async def set(self,*args,**kwargs): raise ConnectionError('cache unavailable')
    with pytest.raises(HTTPException) as exc:
        await enforce_mfa_replay(Broken(),'u','123456')
    assert exc.value.status_code==503


@pytest.mark.asyncio
async def test_database_outage_never_produces_readiness_snapshot():
    class BadPool:
        def connection(self): raise ConnectionError('database unavailable')
    repo=OperationsRepository(SimpleNamespace(pool=BadPool()))
    with pytest.raises(ConnectionError):
        await repo.snapshot()


def test_ambiguous_exchange_status_remains_unknown():
    assert exchange_status({'status':'TRIGGERED'})=='UNKNOWN'
    assert exchange_status({'status':'unexpected'})=='UNKNOWN'
    assert exchange_status({'algoStatus':'FINISHED'},algo=True)=='UNKNOWN'


def test_client_payload_mutation_detected_by_digest():
    a={'symbol':'BTCUSDT','quantity':'0.001'}
    b={**a,'quantity':'0.01'}
    assert digest(a)!=digest(b)


def test_staging_secrets_not_mounted_into_public_web_or_research():
    import yaml
    spec=yaml.safe_load((Path(__file__).parents[1]/'docker-compose.yml').read_text())
    services=spec['services']
    for service in ('gateway','research-worker','paper-worker','collector','outbox','operations-worker'):
        assert '.env.exchange' not in str(services[service].get('env_file',[]))
        assert 'BINANCE_SPOT_TESTNET_KEY' not in str(services[service].get('environment',{}))
        assert 'BINANCE_FUTURES_DEMO_SECRET' not in str(services[service].get('environment',{}))
    assert services['exchange-worker']['env_file'][0]['path']=='.env.exchange'


def test_no_live_gateway_or_ai_order_execution():
    root=Path(__file__).parents[1]/'src/trader'
    assert 'adapter.submit(' not in (root/'intelligence/worker.py').read_text()
    assert 'ENABLE_LIVE_TRADING=true' not in (root/'operations/worker.py').read_text()


def test_closed_candle_admission_and_partial_candle_rejection():
    from trader.market import MarketEvent
    from trader.operations.closed_candles import closed_bar
    payload={'interval':'1m','open_ts':60000,'close_ts':119999,
             'open':'100','high':'105','low':'99','close':'102','volume':'10','closed':False}
    event=MarketEvent('fake','spot','BTCUSDT','kline',110000,120000,payload)
    assert closed_bar(event) is None
    p={**payload,'closed':True}
    good=MarketEvent('fake2','spot','BTCUSDT','kline',120000,120001,p)
    bar=closed_bar(good)
    assert bar is not None and bar.open_ms==60000 and str(bar.c)=='102'
    invalid=MarketEvent('bad','spot','BTCUSDT','kline',120000,120001,{**p,'low':'200'})
    with pytest.raises(ValueError):
        closed_bar(invalid)


def test_research_role_never_has_order_submission_grants():
    from scripts.ops.provision_roles import ROLE_TABLES
    assert 'exchange_order_intents' not in ROLE_TABLES['research']
    assert 'governance_requests' not in ROLE_TABLES['research']
    assert 'exchange_order_intents' not in ROLE_TABLES['operations'] or \
           'INSERT' not in ROLE_TABLES['operations']['exchange_order_intents']


def test_staging_cannot_reuse_database_superuser(monkeypatch):
    from trader.config import Settings
    monkeypatch.setenv('DEPLOYMENT_MODE','staging')
    monkeypatch.setenv('DATABASE_URL','postgresql://trader:pwd@postgres:5432/trader')
    monkeypatch.setenv('ENABLE_LIVE_TRADING','false')
    monkeypatch.setenv('JWT_SECRET','some-very-long-unit-test-secret-value-0000000000000000')
    with pytest.raises(RuntimeError,match='least-privilege'):
        Settings.load()


@pytest.mark.asyncio
async def test_browser_security_headers_on_responses(monkeypatch):
    import httpx
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from trader.operations.security import SecurityHeadersMiddleware

    async def homepage(request):
        return PlainTextResponse('ok')

    monkeypatch.setenv('DEPLOYMENT_MODE','development')
    app=Starlette(routes=[Route('/', homepage)])
    app.add_middleware(SecurityHeadersMiddleware)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        r=await client.get('/')
        assert r.status_code==200
        assert r.headers['Cache-Control']=='no-store'
        assert r.headers['X-Frame-Options']=='DENY'
        assert 'frame-ancestors' in r.headers['Content-Security-Policy']


@pytest.mark.asyncio
async def test_staging_host_not_allowlisted_is_rejected(monkeypatch):
    import httpx
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from trader.operations.security import SecurityHeadersMiddleware

    async def homepage(request):
        return PlainTextResponse('ok')

    monkeypatch.setenv('DEPLOYMENT_MODE','staging')
    monkeypatch.setenv('STAGING_HOST','secure.example.org')
    app=Starlette(routes=[Route('/', homepage)])
    app.add_middleware(SecurityHeadersMiddleware)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://bad.site') as client:
        assert (await client.get('/')).status_code==400
        allowed=await client.get('/',headers={'Host':'secure.example.org'})
        assert allowed.status_code==200
