"""Live module negative-case suite; never calls production network or uses exchange keys."""
from decimal import Decimal
import hashlib
from pathlib import Path
import pytest
from pydantic import ValidationError
from trader.live.settings import LiveSettings, LIVE_HOSTS
from trader.live.contracts import LiveOrder, AutomationConfig
from trader.live.automation import decide
from trader.live.risk import validate_live_order, check_daily_drawdown
from trader.live.adapter import LiveTransport
from trader.live.authorization import KINDS
from trader.exchange.policy import PolicyError

@pytest.fixture
def safe(monkeypatch):
    for k in ['LIVE_TRADING_ENABLED','LIVE_TRADING_CONFIRM','LIVE_MAX_ORDER_USDT',
              'LIVE_MAX_LEVERAGE','LIVE_MAX_DAILY_ORDERS','LIVE_MAX_DAILY_LOSS_PCT',
              'LIVE_POLL_SECONDS','BINANCE_SPOT_LIVE_KEY','BINANCE_SPOT_LIVE_SECRET',
              'LIVE_SPOT_KEY_SHA256']:
        monkeypatch.delenv(k,raising=False)
    monkeypatch.setenv('DEPLOYMENT_MODE','production')

@pytest.mark.parametrize('key,value',[('LIVE_MAX_ORDER_USDT','101'),('LIVE_MAX_LEVERAGE','3'),
 ('LIVE_MAX_DAILY_ORDERS','21'),('LIVE_MAX_DAILY_LOSS_PCT','3'),('LIVE_POLL_SECONDS','1')])
def test_canary_caps_not_configurable_beyond_hard_limits(safe,monkeypatch,key,value):
    monkeypatch.setenv(key,value)
    with pytest.raises(ValueError):LiveSettings.load()

def test_live_requires_explicit_production_phrase(safe,monkeypatch):
    assert not LiveSettings.load().enabled
    monkeypatch.setenv('LIVE_TRADING_ENABLED','true')
    with pytest.raises(RuntimeError):LiveSettings.load()
    monkeypatch.setenv('LIVE_TRADING_CONFIRM','I_AUTHORIZE_CONTROLLED_REAL_MONEY_TRADING')
    assert LiveSettings.load().enabled
    monkeypatch.setenv('DEPLOYMENT_MODE','staging')
    with pytest.raises(RuntimeError):LiveSettings.load()

def test_fingerprint_binds_specific_api_key(safe,monkeypatch):
    monkeypatch.setenv('LIVE_TRADING_ENABLED','true')
    monkeypatch.setenv('LIVE_TRADING_CONFIRM','I_AUTHORIZE_CONTROLLED_REAL_MONEY_TRADING')
    monkeypatch.setenv('BINANCE_SPOT_LIVE_KEY','abc')
    monkeypatch.setenv('BINANCE_SPOT_LIVE_SECRET','secret')
    monkeypatch.setenv('LIVE_SPOT_KEY_SHA256','0'*64)
    assert not LiveSettings.load().configured('spot-live')
    monkeypatch.setenv('LIVE_SPOT_KEY_SHA256',hashlib.sha256(b'abc').hexdigest())
    assert LiveSettings.load().configured('spot-live')

def test_production_hosts_pinned_and_no_sandbox_proxy():
    assert LIVE_HOSTS=={'spot-live':'https://api.binance.com','futures-live':'https://fapi.binance.com'}
    with pytest.raises(ValueError):LiveTransport('spot-testnet','x','y')
    with pytest.raises(ValueError):LiveTransport('http://example.com','x','y')

@pytest.mark.parametrize('bad',[{'quantity':'NaN'},{'quantity':'-1'},
 {'protective_stop':'90'},{'reduce_only':True},{'type':'LIMIT'}])
def test_spot_live_payload_rejects_dangerous_shapes(bad):
    values={'account':'spot-live','client_order_id':'at-12345678','symbol':'BTCUSDT',
            'side':'BUY','type':'MARKET','quantity':'0.001'}
    with pytest.raises(ValidationError):LiveOrder(**{**values,**bad})

def test_futures_requires_stop_and_ignores_limits():
    values={'account':'futures-live','client_order_id':'at-12345678','symbol':'BTCUSDT',
            'side':'BUY','type':'MARKET','quantity':'0.001'}
    with pytest.raises(ValidationError):LiveOrder(**values)
    assert LiveOrder(**{**values,'protective_stop':'29000'}).protective_stop=='29000'
    with pytest.raises(ValidationError):LiveOrder(**{**values,'reduce_only':True,'protective_stop':'29000'})
    with pytest.raises(ValidationError):LiveOrder(**{**values,'type':'LIMIT','price':'30000','protective_stop':'29000'})

def test_fixed_sma_strategy_does_not_call_ai():
    closes=[Decimal(i) for i in range(1,41)]
    assert decide(closes[:20],8,30,Decimal(0)) is None
    assert decide(closes,8,30,Decimal(0))=='BUY'
    assert decide(closes,8,30,Decimal(1)) is None
    assert decide(closes[::-1],8,30,Decimal(1))=='SELL'
    with pytest.raises(ValidationError):AutomationConfig(symbol='BTCUSDT',quantity='-1')

def test_loss_beyond_daily_limit_rejected():
    check_daily_drawdown('990','1000','1')
    with pytest.raises(PolicyError,match='DAILY_DRAWDOWN_LIMIT'):
        check_daily_drawdown('989','1000','1')
    with pytest.raises(PolicyError):check_daily_drawdown('100','0','1')

def test_futures_stop_has_strict_distance_and_position_guard():
    info={'symbols':[{'symbol':'BTCUSDT','status':'TRADING','quoteAsset':'USDT',
      'baseAsset':'BTC','filters':[{'filterType':'LOT_SIZE','minQty':'0.0001',
      'maxQty':'100','stepSize':'0.0001'},{'filterType':'MIN_NOTIONAL','minNotional':'5'}]}]}
    quote={'bidPrice':'30000','askPrice':'30000.1'}
    pos={'symbol':'BTCUSDT','positionAmt':'0','positionSide':'BOTH',
         'marginType':'isolated','leverage':2}
    snap={'positions':[pos],'position_mode':{'dualSidePosition':False},
          'account':{'availableBalance':'500'}}
    class Cfg:
        symbols=('BTCUSDT',)
        max_order_usdt=Decimal('50')
        max_leverage=2
        def configured(self,code):return True
    payload={'symbol':'BTCUSDT','side':'BUY','quantity':'0.001','type':'MARKET',
             'protective_stop':'29600','reduce_only':False}
    assert validate_live_order('futures-live',payload,info,quote,snap,Cfg())['exchange']=='futures-live'
    with pytest.raises(PolicyError,match='PROTECTIVE_STOP_DISTANCE'):
        validate_live_order('futures-live',{**payload,'protective_stop':'31000'},info,quote,snap,Cfg())
    with pytest.raises(PolicyError,match='NO_NEW_FUTURES_EXPOSURE'):
        validate_live_order('futures-live',payload,info,quote,
            {**snap,'positions':[{**pos,'positionAmt':'0.001'}]},Cfg())

def test_control_plane_not_credential_bearing_or_direct_writer():
    source=Path('src/trader/live/http_api.py').read_text()
    assert 'LiveTransport' not in source and '.submit(' not in source
    assert 'BINANCE_SPOT_LIVE_SECRET' not in source
    compose=Path('infrastructure/production/docker-compose.production.yml').read_text()
    assert 'profiles: [live]' in compose
    assert 'required: true' in compose
    assert 'LIVE_TRADING_ENABLED: ${LIVE_TRADING_ENABLED:-false}' in compose
    assert 'ROLE_PASSWORD_LIVE' in compose

def test_model_agents_cannot_authorize_live_orders():
    assert KINDS=={'SPOT_ARM','FUTURES_ARM','SPOT_AUTOMATION','SPOT_ORDER','FUTURES_ORDER'}
    worker=Path('src/trader/live/worker.py').read_text()
    assert 'trader.intelligence' not in worker
