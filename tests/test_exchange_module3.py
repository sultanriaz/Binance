"""Offline P15-P19 contract, risk, egress and failure-path coverage."""
import hashlib
import hmac
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from trader.exchange.adapters import SandboxAdapter
from trader.exchange.engine import ReconcileError, SandboxEngine, exchange_status
from trader.exchange.contracts import ExchangeOrder
from trader.exchange.policy import PolicyError, validate_order
from trader.exchange.repository import digest
from trader.exchange.settings import ExchangeSettings, HOSTS
from trader.exchange.streams import event_key
from trader.exchange.transport import ExchangeError, SandboxTransport, UncertainDelivery


def policy_fixtures():
    info={'symbols':[{'symbol':'BTCUSDT','status':'TRADING','baseAsset':'BTC','quoteAsset':'USDT','filters':[
        {'filterType':'LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'100'},
        {'filterType':'MARKET_LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'100'},
        {'filterType':'PRICE_FILTER','tickSize':'0.01'},
        {'filterType':'MIN_NOTIONAL','minNotional':'5'}]}]}
    quote={'symbol':'BTCUSDT','bidPrice':'10000','askPrice':'10001'}
    spot={'account':{'balances':[{'asset':'BTC','free':'2'}, {'asset':'USDT','free':'10000'}]},
          'orders':[],'algos':[],'positions':[],'position_mode':None}
    futures={'account':{'availableBalance':'1000'},'orders':[],'algos':[],
             'positions':[{'symbol':'BTCUSDT','positionSide':'BOTH','positionAmt':'0',
                           'marginType':'isolated','leverage':'2'}],
             'position_mode':{'dualSidePosition':False}}
    return info,quote,spot,futures


def order(**kwargs):
    o={'symbol':'BTCUSDT','side':'BUY','type':'MARKET','quantity':'0.001','reduce_only':False}
    o.update(kwargs)
    return o


def check(code, payload, snap):
    info,quote,_,_=policy_fixtures()
    return validate_order(code,payload,info,quote,snap,
                          symbols=('BTCUSDT',),max_notional=50,max_leverage=2)


def test_endpoint_allowlist_and_segregated_pinning(monkeypatch):
    assert all('binance.vision' in h[0] or 'demo-fapi' in h[0] for h in HOSTS.values())
    with pytest.raises(ValueError):
        SandboxTransport('production','key','secret')
    monkeypatch.setenv('ENABLE_LIVE_TRADING','true')
    with pytest.raises(RuntimeError):
        ExchangeSettings.load()
    monkeypatch.delenv('ENABLE_LIVE_TRADING')
    monkeypatch.setenv('ENABLE_TESTNET_EXECUTION','true')
    monkeypatch.setenv('TESTNET_CONFIRM','NOT_CONFIRMED')
    assert not ExchangeSettings.load().configured('spot-testnet')


def test_hmac_signature_is_exactly_query_string_bytes():
    query,signature=SandboxTransport.signature('s3cr3t',{'symbol':'BTCUSDT','side':'BUY'})
    assert query=='symbol=BTCUSDT&side=BUY'
    assert signature==hmac.new(b's3cr3t',query.encode(),hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_transport_signing_and_prevent_redirects():
    seen=[]
    def respond(req):
        seen.append(req)
        if req.url.path.endswith('/account'):
            return httpx.Response(200,json={'ok':True})
        return httpx.Response(307,headers={'Location':'https://api.binance.com/api/v3/order'})
    c=httpx.AsyncClient(transport=httpx.MockTransport(respond))
    t=SandboxTransport('spot-testnet','api-key','secret',client=c)
    assert (await t.request('GET','/api/v3/account',signed=True))['ok']
    assert seen[0].url.host=='testnet.binance.vision'
    assert seen[0].headers['X-MBX-APIKEY']=='api-key'
    assert 'signature=' in str(seen[0].url)
    with pytest.raises(ExchangeError,match='redirect'):
        await t.request('POST','/api/v3/order',{'symbol':'BTCUSDT'},signed=True)
    await c.aclose()


@pytest.mark.asyncio
async def test_uncertain_write_not_retried():
    n=0
    def fail(req):
        nonlocal n
        n+=1
        raise httpx.ReadTimeout('Request may be accepted')
    c=httpx.AsyncClient(transport=httpx.MockTransport(fail))
    t=SandboxTransport('spot-testnet','api-key','secret',client=c)
    with pytest.raises(UncertainDelivery):
        await t.request('POST','/api/v3/order',{'symbol':'BTCUSDT'},signed=True)
    assert n == 1
    await c.aclose()


@pytest.mark.asyncio
async def test_http_5xx_unknown_vs_4xx_explicit_rejection():
    async def calls(status):
        c=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r:httpx.Response(status,json={'code':-1013,'msg':'bad filter'})))
        try:
            return await SandboxTransport('futures-demo','k','s',client=c).request('POST','/fapi/v1/order',signed=True)
        finally: await c.aclose()
    with pytest.raises(UncertainDelivery):
        await calls(503)
    with pytest.raises(ExchangeError) as exc:
        await calls(400)
    assert exc.value.code == -1013


@pytest.mark.parametrize('bad',[
    {'symbol':'DOGEUSDT'}, {'quantity':'0'}, {'quantity':'0.0001'},
    {'quantity':'Infinity'}, {'quantity':'0.0201'}, {'quantity':'1'},
    {'price':'8000','type':'LIMIT'}, {'price':'10000.005','type':'LIMIT'},
])
def test_spot_risk_rejections(bad):
    _,_,spot,_=policy_fixtures()
    with pytest.raises(PolicyError):
        check('spot-testnet',order(**bad),spot)


def test_spot_valid_inventory_and_balance_constraints():
    _,_,spot,_=policy_fixtures()
    assert check('spot-testnet',order(),spot)['estimated_notional']=='10.001'
    assert check('spot-testnet',order(side='SELL'),spot)['exchange']=='spot-testnet'
    spot['account']['balances'][0]['free']='0'
    with pytest.raises(PolicyError,match='INSUFFICIENT_SPOT_INVENTORY'):
        check('spot-testnet',order(side='SELL'),spot)


@pytest.mark.parametrize('mod,reason',[
    (lambda s:s['position_mode'].update(dualSidePosition=True),'HEDGE_MODE_UNSUPPORTED'),
    (lambda s:s['positions'][0].update(marginType='cross'),'ISOLATED_MARGIN_REQUIRED'),
    (lambda s:s['positions'][0].update(leverage='5'),'LEVERAGE_TOO_HIGH'),
    (lambda s:s['account'].update(availableBalance='0'),'INSUFFICIENT_FUTURES_MARGIN_BUFFER'),
])
def test_futures_configuration_blocks(mod,reason):
    _,_,_,f=policy_fixtures()
    mod(f)
    with pytest.raises(PolicyError,match=reason):
        check('futures-demo',order(),f)


def test_futures_protective_order_must_reduce_existing_position():
    _,_,_,f=policy_fixtures()
    f['positions'][0]['positionAmt']='0.002'
    payload=order(side='SELL',type='STOP_MARKET',trigger_price='9900',reduce_only=True)
    assert check('futures-demo',payload,f)['exchange']=='futures-demo'
    with pytest.raises(PolicyError,match='REDUCE_ONLY_NOT_REDUCING'):
        check('futures-demo',order(side='BUY',type='STOP_MARKET',trigger_price='9900',reduce_only=True),f)
    with pytest.raises(PolicyError,match='FUTURES_CONDITIONAL_MUST_REDUCE'):
        check('futures-demo',order(side='SELL',type='STOP_MARKET',trigger_price='9900'),f)


def test_request_contracts_and_different_idempotency_fingerprints():
    b=ExchangeOrder(account='spot-testnet',client_order_id='abc123456',**order())
    assert b.model_dump()['account']=='spot-testnet'
    with pytest.raises(ValidationError):
        ExchangeOrder(account='futures-demo',client_order_id='abc123456',
                      **order(type='STOP_MARKET',trigger_price='9900'))
    with pytest.raises(ValidationError):
        ExchangeOrder(account='spot-testnet',client_order_id='abc123456',**order(type='STOP_MARKET'))
    assert digest(order()) == digest(dict(reversed(list(order().items()))))
    assert digest(order()) != digest(order(side='SELL'))


@pytest.mark.asyncio
async def test_spot_order_routes_and_futures_algo_routes():
    seen=[]
    def handle(req):
        seen.append((req.method,req.url.path,req.content.decode()))
        return httpx.Response(200,json={'clientOrderId':'myid','status':'NEW'})
    c=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        spot=SandboxAdapter(SandboxTransport('spot-testnet','key','secret',client=c))
        fut=SandboxAdapter(SandboxTransport('futures-demo','key','secret',client=c))
        await spot.submit(order(), 'spot123456')
        await spot.query('BTCUSDT','spot123456')
        await spot.cancel('BTCUSDT','spot123456')
        await fut.submit(order(side='SELL',type='STOP_MARKET',trigger_price='9900',reduce_only=True),'fut123456')
        await fut.query('BTCUSDT','fut123456',algo=True)
        await fut.cancel('BTCUSDT','fut123456',algo=True)
        assert [x[:2] for x in seen] == [
            ('POST','/api/v3/order'),('GET','/api/v3/order'),('DELETE','/api/v3/order'),
            ('POST','/fapi/v1/algoOrder'),('GET','/fapi/v1/algoOrder'),('DELETE','/fapi/v1/algoOrder')]
        assert 'clientAlgoId=fut123456' in seen[3][2]
        assert 'reduceOnly=true' in seen[3][2]
    finally: await c.aclose()


def test_event_key_dedup_and_algo_status_not_assumed_filled():
    e={'e':'ORDER_TRADE_UPDATE','E':1,'o':{'i':999,'t':42}}
    assert event_key(e)==event_key(deepcopy(e))
    e['o']['t']=43
    assert event_key(e)!=event_key({'e':'ORDER_TRADE_UPDATE','E':1,'o':{'i':999,'t':42}})
    assert exchange_status({'algoStatus':'TRIGGERED'},algo=True) != 'FILLED'


class MemoryRepo:
    def __init__(self, items=None):
        self.items=items or []
        self.states=[]
        self.issues=[]
        self.enabled_flag=True
        self.statuses=[]
    async def mark_account(self,code,state,**kwargs):
        self.states.append(state)
    async def intents(self,code,limit=50):
        return self.items
    async def cursors(self,code):
        return {}
    async def add_fills(self,*args):
        pass
    async def open_incidents(self,code):
        return self.issues
    async def incident(self,code,category,detail=None):
        self.issues.append({'category':category})
        self.states.append('INCIDENT')
    async def enabled(self,code):
        return self.enabled_flag
    async def count_today(self,code):
        return 0
    async def set_status(self,ident,status,response=None,**kwargs):
        self.statuses.append(status)
    async def claim_queued(self,code,intent_id):
        x=next((i for i in self.items if i['status']=='QUEUED' and i['id']==intent_id),None)
        if x:
            x['status']='SUBMITTING'
        return x


class FakeAdapter:
    code='spot-testnet'
    def __init__(self):
        self.submissions=0
        _,self.q,self.spot,_=policy_fixtures()
    async def snapshot(self):
        return self.spot
    async def query(self,symbol,client_id,algo=False):
        return {'status':'FILLED','orderId':123}
    async def trades(self,symbol,from_id=None):
        return []
    async def instruments(self):
        return policy_fixtures()[0]
    async def quote(self,symbol):
        return self.q
    def is_algo(self,payload):
        return False
    async def submit(self,payload,client_id):
        self.submissions+=1
        return {'status':'NEW','orderId':999}


def fake_settings():
    return SimpleNamespace(symbols=('BTCUSDT',),max_order_usdt=50,max_daily_orders=20,
                           max_leverage=2,configured=lambda code:True)


@pytest.mark.asyncio
async def test_reconcile_orphan_exchange_order_halts():
    a=FakeAdapter()
    a.spot['orders']=[{'clientOrderId':'EXTERNAL_ORDER','symbol':'BTCUSDT'}]
    r=MemoryRepo()
    engine=SandboxEngine('spot-testnet',a,r,fake_settings())
    with pytest.raises(ReconcileError,match='UNMANAGED_EXCHANGE_OPEN_ORDERS'):
        await engine.reconcile()
    assert 'INCIDENT' in r.states


@pytest.mark.asyncio
async def test_reconcile_unknown_order_blocks_new_submission():
    a=FakeAdapter()
    r=MemoryRepo([{'id':'uuid1','client_order_id':'cid123456',
          'payload':order(),'status':'UNKNOWN','created_at':1}])
    async def missing(*args,**kwargs):
        raise ExchangeError('Not found',-2013)
    a.query=missing
    engine=SandboxEngine('spot-testnet',a,r,fake_settings())
    with pytest.raises(ReconcileError,match='AMBIGUOUS_ORDER_OUTCOME'):
        await engine.process_once()
    assert a.submissions==0


@pytest.mark.asyncio
async def test_ready_reconcile_then_single_queued_order():
    a=FakeAdapter()
    r=MemoryRepo([{'id':'uuid1','client_order_id':'cid123456',
       'payload':order(),'status':'QUEUED','created_at':1,'cancel_requested':False}])
    e=SandboxEngine('spot-testnet',a,r,fake_settings())
    result=await e.process_once()
    assert result=='submitted'
    assert a.submissions==1
    assert r.statuses[-1]=='NEW'
    assert 'READY' in r.states


@pytest.mark.asyncio
async def test_disabled_operator_no_submissions_even_when_reconciled():
    a=FakeAdapter()
    r=MemoryRepo([{'id':'uuid1','client_order_id':'cid123456',
       'payload':order(),'status':'QUEUED','created_at':1,'cancel_requested':False}])
    r.enabled_flag=False
    result=await SandboxEngine('spot-testnet',a,r,fake_settings()).process_once()
    assert result=='disarmed'
    assert a.submissions==0


@pytest.mark.asyncio
async def test_transport_timeout_turns_into_incident_not_retry():
    a=FakeAdapter()
    r=MemoryRepo([{'id':'uuid1','client_order_id':'cid123456',
       'payload':order(),'status':'QUEUED','created_at':1,'cancel_requested':False}])
    async def timeout(*args,**kwargs):
        a.submissions+=1
        raise UncertainDelivery('unknown')
    a.submit=timeout
    with pytest.raises(ReconcileError,match='UNDETERMINED_SUBMISSION'):
        await SandboxEngine('spot-testnet',a,r,fake_settings()).process_once()
    assert a.submissions==1
    assert 'UNKNOWN' in r.statuses
    assert 'INCIDENT' in r.states

@pytest.mark.asyncio
async def test_triggered_futures_algo_requires_downstream_order_evidence():
    class Triggered(FakeAdapter):
        code = 'futures-demo'
        def is_algo(self, payload):
            return True
        async def query(self, symbol, client_id, algo=False):
            return {'algoStatus': 'TRIGGERED', 'algoId': 555, 'actualOrderId': 999}
        async def query_regular_by_id(self, symbol, order_id):
            assert order_id == 999
            return {'status': 'FILLED', 'orderId': 999}
    r = MemoryRepo()
    item = {'id': 'i1', 'client_order_id': 'futures-test-01',
            'payload': order(side='SELL', type='STOP_MARKET', trigger_price='9800', reduce_only=True)}
    assert await SandboxEngine('futures-demo', Triggered(), r, fake_settings())._observe(item) == 'FILLED'
    assert r.statuses == ['FILLED']


@pytest.mark.asyncio
async def test_triggered_algo_without_downstream_evidence_remains_unknown():
    class Ambiguous(FakeAdapter):
        code = 'futures-demo'
        def is_algo(self, payload):
            return True
        async def query(self, symbol, client_id, algo=False):
            return {'algoStatus': 'TRIGGERED', 'algoId': 123}
    r = MemoryRepo()
    item = {'id': 'i1', 'client_order_id': 'futures-test-02',
            'payload': order(side='SELL', type='STOP_MARKET', trigger_price='9800', reduce_only=True)}
    assert await SandboxEngine('futures-demo', Ambiguous(), r, fake_settings())._observe(item) == 'UNKNOWN'
    assert r.statuses == []


@pytest.mark.asyncio
async def test_cancel_submission_never_claims_exchange_cancel_confirmed():
    class Cancellable(FakeAdapter):
        async def query(self, symbol, client_id, algo=False):
            return {'status': 'NEW', 'orderId': 555}
        async def cancel(self, symbol, client_id, algo=False):
            return {'status': 'CANCELED', 'orderId': 555}
    a = Cancellable()
    r = MemoryRepo([{'id': 'i1', 'client_order_id': 'spot-cancel-01',
         'payload': order(type='LIMIT', price='10000'), 'status': 'NEW',
         'cancel_requested': True, 'created_at': 1}])
    assert await SandboxEngine('spot-testnet', a, r, fake_settings()).process_once() == 'cancelled'
    assert r.statuses[-1] == 'CANCEL_PENDING'
    assert 'CANCELED' not in r.statuses
