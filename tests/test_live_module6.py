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


def test_quantize_helpers():
    from trader.exchange.policy import quantize_down, quantize_format
    assert quantize_down(Decimal('0.0058'), Decimal('0.001')) == Decimal('0.005')
    assert quantize_down('0.0058', '0.001') == Decimal('0.005')
    assert quantize_format('0.0058', '0.001') == '0.005'
    assert quantize_format('100.456', '0.1') == '100.4'
    assert quantize_format('10', '1') == '10'


@pytest.mark.asyncio
async def test_spot_equity_handles_bnb_and_filters_dust():
    from trader.live.engine import LiveEngine, LiveExecutionHalt
    class FakeAdapter:
        async def instruments(self):
            return {'symbols': [
                {'symbol': 'BTCUSDT', 'baseAsset': 'BTC', 'quoteAsset': 'USDT'},
                {'symbol': 'BNBUSDT', 'baseAsset': 'BNB', 'quoteAsset': 'USDT'},
                {'symbol': 'DOGEUSDT', 'baseAsset': 'DOGE', 'quoteAsset': 'USDT'},
            ]}
        async def quote(self, symbol):
            if symbol == 'BTCUSDT': return {'bidPrice': '60000'}
            if symbol == 'BNBUSDT': return {'bidPrice': '600'}
            if symbol == 'DOGEUSDT': return {'bidPrice': '0.10'}
            raise ValueError(f'unknown symbol {symbol}')

    class FakeSettings:
        symbols = ('BTCUSDT',)

    engine = LiveEngine('spot-live', FakeAdapter(), None, FakeSettings())
    # Snapshot holding: 100 USDT, 0.001 BTC ($60), 0.05 BNB ($30), 2 DOGE ($0.20 - dust), 0.000001 PEPE (untracked dust)
    snapshot = {
        'account': {
            'balances': [
                {'asset': 'USDT', 'free': '100', 'locked': '0'},
                {'asset': 'BTC', 'free': '0.001', 'locked': '0'},
                {'asset': 'BNB', 'free': '0.05', 'locked': '0'},
                {'asset': 'DOGE', 'free': '2', 'locked': '0'},       # $0.20 < $1.00 dust
                {'asset': 'PEPE', 'free': '0.00001', 'locked': '0'}, # trivial untracked dust
            ]
        }
    }
    equity = await engine._spot_equity(snapshot)
    # 100 (USDT) + 60 (BTC) + 30 (BNB) = 190 USDT
    assert equity == Decimal('190')

    # Large untracked asset must halt
    bad_snapshot = {
        'account': {
            'balances': [
                {'asset': 'USDT', 'free': '100', 'locked': '0'},
                {'asset': 'XRP', 'free': '5000', 'locked': '0'},
            ]
        }
    }
    with pytest.raises(LiveExecutionHalt, match='SPOT_BALANCE_CANNOT_BE_VALUED_XRP'):
        await engine._spot_equity(bad_snapshot)


@pytest.mark.asyncio
async def test_emergency_flatten_futures_dispatches_reduce_only():
    from trader.live.engine import LiveEngine
    submitted = []
    incidents = []

    class FakeAdapter:
        async def submit(self, payload, client_id):
            submitted.append((payload, client_id))
            return {'orderId': 99999, 'status': 'NEW'}

    class FakeRepo:
        async def incident(self, code, reason, detail):
            incidents.append((code, reason, detail))

    engine = LiveEngine('futures-live', FakeAdapter(), FakeRepo(), None)
    res = await engine._emergency_flatten_futures('BTCUSDT', 'SELL', '0.005', 'STOP_FAILED')
    assert res['orderId'] == 99999
    assert len(submitted) == 1
    order_payload, client_id = submitted[0]
    assert order_payload['symbol'] == 'BTCUSDT'
    assert order_payload['side'] == 'SELL'
    assert order_payload['type'] == 'MARKET'
    assert order_payload['reduce_only'] is True
    assert order_payload['quantity'] == '0.005'
    assert client_id.startswith('e')
    assert len(incidents) == 1
    assert incidents[0][1] == 'EMERGENCY_UNWIND_SUBMITTED'


def test_futures_bracket_order_validation():
    # Valid Long Bracket: Stop below entry, Take Profit above entry
    valid_long = LiveOrder(
        account='futures-live', client_order_id='at-12345678', symbol='BTCUSDT',
        side='BUY', type='MARKET', quantity='0.001',
        protective_stop='29000', take_profit='31000'
    )
    assert valid_long.take_profit == '31000'

    # Invalid Long: Take profit below stop
    with pytest.raises(ValidationError, match='Take profit must be above protective stop for BUY'):
        LiveOrder(
            account='futures-live', client_order_id='at-12345678', symbol='BTCUSDT',
            side='BUY', type='MARKET', quantity='0.001',
            protective_stop='29000', take_profit='28000'
        )

    # Valid Short Bracket: Stop above entry, Take Profit below entry
    valid_short = LiveOrder(
        account='futures-live', client_order_id='at-12345678', symbol='BTCUSDT',
        side='SELL', type='MARKET', quantity='0.001',
        protective_stop='31000', take_profit='29000'
    )
    assert valid_short.take_profit == '29000'

    # Spot must reject take_profit
    with pytest.raises(ValidationError, match='Spot does not accept'):
        LiveOrder(
            account='spot-live', client_order_id='at-12345678', symbol='BTCUSDT',
            side='BUY', type='MARKET', quantity='0.001', take_profit='31000'
        )


def test_funding_rate_drift_rejection():
    from trader.live.risk import check_funding_rate_drift
    # Normal funding rate (0.01%)
    check_funding_rate_drift('BTCUSDT', 'BUY', {'lastFundingRate': '0.0001'})
    check_funding_rate_drift('BTCUSDT', 'SELL', {'lastFundingRate': '-0.0001'})

    # Extreme positive funding (+0.08% > 0.05% ceiling) rejects BUY (Long pays heavily)
    with pytest.raises(PolicyError, match='ADVERSE_FUNDING_RATE_TOO_HIGH'):
        check_funding_rate_drift('BTCUSDT', 'BUY', {'lastFundingRate': '0.0008'})

    # Extreme negative funding (-0.08% < -0.05% floor) rejects SELL (Short pays heavily)
    with pytest.raises(PolicyError, match='ADVERSE_FUNDING_RATE_TOO_HIGH'):
        check_funding_rate_drift('BTCUSDT', 'SELL', {'lastFundingRate': '-0.0008'})


def test_liquidation_watchdog_proximity():
    from trader.live.risk import check_liquidation_proximity
    # Safe Long: Mark 80000, Liquidation 40000 (50% distance)
    safe_long = {'positionAmt': '0.005', 'markPrice': '80000', 'liquidationPrice': '40000'}
    assert not check_liquidation_proximity(safe_long, Decimal('0.03'))

    # Critical Long: Mark 80000, Liquidation 78500 (1.8% distance < 3% threshold)
    critical_long = {'positionAmt': '0.005', 'markPrice': '80000', 'liquidationPrice': '78500'}
    assert check_liquidation_proximity(critical_long, Decimal('0.03'))

    # Critical Short: Mark 80000, Liquidation 81500 (1.8% distance < 3% threshold)
    critical_short = {'positionAmt': '-0.005', 'markPrice': '80000', 'liquidationPrice': '81500'}
    assert check_liquidation_proximity(critical_short, Decimal('0.03'))

    # Flat position (0 amt) must not trigger
    flat = {'positionAmt': '0', 'markPrice': '80000', 'liquidationPrice': '80000'}
    assert not check_liquidation_proximity(flat, Decimal('0.03'))


@pytest.mark.asyncio
async def test_futures_account_auto_initialization():
    from trader.live.engine import LiveEngine
    calls = []

    class FakeAdapter:
        async def set_one_way_mode(self):
            calls.append('one_way_mode')
            return {'code': 200, 'msg': 'Already in one-way mode'}
        async def set_isolated_margin(self, symbol):
            calls.append(f'isolated_{symbol}')
            return {'code': 200, 'msg': 'Already isolated'}
        async def set_leverage(self, symbol, leverage):
            calls.append(f'leverage_{symbol}_{leverage}')
            return {'code': 200}

    class FakeSettings:
        symbols = ('BTCUSDT', 'ETHUSDT')
        max_leverage = 2

    engine = LiveEngine('futures-live', FakeAdapter(), None, FakeSettings())
    await engine.initialize_futures_account()
    assert 'one_way_mode' in calls
    assert 'isolated_BTCUSDT' in calls
    assert 'isolated_ETHUSDT' in calls
    assert 'leverage_BTCUSDT_2' in calls
    assert 'leverage_ETHUSDT_2' in calls


@pytest.mark.asyncio
async def test_verify_api_restrictions_enforcement():
    from trader.live.risk import verify_api_restrictions

    class FakeAdapter:
        def __init__(self, restrictions):
            self._restrictions = restrictions
        async def get_api_restrictions(self):
            return self._restrictions

    # 1. Withdrawals enabled must be rejected immediately
    bad_withdrawals = {
        'ipRestrict': True, 'enableWithdrawals': True,
        'enableSpotAndMarginTrading': True, 'enableFutures': True
    }
    with pytest.raises(RuntimeError, match='WITHDRAWALS ENABLED'):
        await verify_api_restrictions(FakeAdapter(bad_withdrawals), 'spot-live')

    # 2. Unrestricted IP must be rejected
    bad_ip = {
        'ipRestrict': False, 'enableWithdrawals': False,
        'enableSpotAndMarginTrading': True, 'enableFutures': True
    }
    with pytest.raises(RuntimeError, match='NOT IP-restricted'):
        await verify_api_restrictions(FakeAdapter(bad_ip), 'spot-live')

    # 3. Missing spot permission on spot-live
    no_spot = {
        'ipRestrict': True, 'enableWithdrawals': False,
        'enableSpotAndMarginTrading': False, 'enableFutures': True
    }
    with pytest.raises(RuntimeError, match='lacks spot trading permissions'):
        await verify_api_restrictions(FakeAdapter(no_spot), 'spot-live')

    # 4. Missing futures permission on futures-live
    no_futures = {
        'ipRestrict': True, 'enableWithdrawals': False,
        'enableSpotAndMarginTrading': True, 'enableFutures': False
    }
    with pytest.raises(RuntimeError, match='lacks futures trading permissions'):
        await verify_api_restrictions(FakeAdapter(no_futures), 'futures-live')

    # 5. Valid restrictions must succeed
    valid = {
        'ipRestrict': True, 'enableWithdrawals': False,
        'enableSpotAndMarginTrading': True, 'enableFutures': True
    }
    res = await verify_api_restrictions(FakeAdapter(valid), 'spot-live')
    assert res['ipRestrict'] is True
    assert res['enableWithdrawals'] is False


def test_bnb_fee_buffer_check():
    from trader.live.risk import check_bnb_fee_buffer

    # Sufficient balance >= 0.05 BNB
    balances_ok = [{'asset': 'USDT', 'free': '1000'}, {'asset': 'BNB', 'free': '0.1'}]
    res_ok = check_bnb_fee_buffer(balances_ok)
    assert res_ok['sufficient_fee_buffer'] is True
    assert res_ok['discount_active'] is True
    assert res_ok['warning'] is None

    # Insufficient balance < 0.05 BNB
    balances_low = [{'asset': 'USDT', 'free': '1000'}, {'asset': 'BNB', 'free': '0.01'}]
    res_low = check_bnb_fee_buffer(balances_low)
    assert res_low['sufficient_fee_buffer'] is False
    assert res_low['discount_active'] is False
    assert 'BNB balance 0.01 < 0.05' in res_low['warning']


@pytest.mark.asyncio
async def test_panic_flatten_futures():
    from trader.live.engine import LiveEngine

    actions = []

    class FakeFuturesAdapter:
        async def cancel_all_symbol_orders(self, symbol):
            actions.append(('cancel_all', symbol))
            return {'code': 200}
        async def snapshot(self):
            return {
                'positions': [
                    {'symbol': 'BTCUSDT', 'positionAmt': '0.005', 'markPrice': '60000'},
                    {'symbol': 'ETHUSDT', 'positionAmt': '-0.1', 'markPrice': '3000'},
                    {'symbol': 'SOLUSDT', 'positionAmt': '0', 'markPrice': '150'},
                ]
            }
        async def submit(self, payload, client_id):
            actions.append(('submit', payload['symbol'], payload['side'], payload['quantity'], payload['reduce_only']))
            return {'status': 'FILLED'}

    class FakeRepo:
        def __init__(self):
            self.incidents_recorded = []
        async def incident(self, code, reason, detail):
            self.incidents_recorded.append((code, reason, detail))

    class FakeSettings:
        symbols = ('BTCUSDT', 'ETHUSDT')

    repo = FakeRepo()
    engine = LiveEngine('futures-live', FakeFuturesAdapter(), repo, FakeSettings())
    res = await engine.panic_flatten()

    assert ('cancel_all', 'BTCUSDT') in actions
    assert ('cancel_all', 'ETHUSDT') in actions
    # BTCUSDT was +0.005, unwind side SELL
    assert ('submit', 'BTCUSDT', 'SELL', '0.005', True) in actions
    # ETHUSDT was -0.1, unwind side BUY
    assert ('submit', 'ETHUSDT', 'BUY', '0.1', True) in actions
    # Incident logged
    assert any(r[1] == 'PANIC_FLATTEN_EXECUTED' for r in repo.incidents_recorded)


@pytest.mark.asyncio
async def test_alert_dispatcher(monkeypatch):
    from trader.live.alerts import send_alert

    # Without webhook URL, returns False and logs without error
    monkeypatch.delenv('ALERT_WEBHOOK_URL', raising=False)
    monkeypatch.delenv('DISCORD_WEBHOOK_URL', raising=False)
    monkeypatch.delenv('SLACK_WEBHOOK_URL', raising=False)
    assert await send_alert("TEST_TITLE", "Test alert message", level="INFO") is False


def test_stream_event_key_hashing():
    from trader.live.streams import event_key

    e1 = {'e': 'ORDER_TRADE_UPDATE', 'E': 1600000000, 'o': {'i': 12345, 'X': 'FILLED'}}
    e2 = {'e': 'ORDER_TRADE_UPDATE', 'E': 1600000000, 'o': {'i': 12345, 'X': 'FILLED'}}
    e3 = {'e': 'ORDER_TRADE_UPDATE', 'E': 1600000001, 'o': {'i': 12345, 'X': 'FILLED'}}

    k1 = event_key(e1)
    k2 = event_key(e2)
    k3 = event_key(e3)

    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 64


def test_calculate_kelly_size():
    from trader.live.automation import calculate_kelly_size

    # 1. Full Kelly f* = (0.65*1.5 - 0.35)/1.5 = 0.41666667
    #    Quarter-Kelly f_safe = 0.10416667 -> 10.416667 clamped & quantized by 0.01 -> 10.41
    size1 = calculate_kelly_size(0.65, 1.5, Decimal('100.0'), Decimal('0.01'))
    assert size1 == Decimal('10.41')

    # 2. Negative expectancy: prob 0.40, payoff 1.0 -> f* = (0.4 - 0.6)/1.0 = -0.2 <= 0 -> 0 size
    size2 = calculate_kelly_size(0.40, 1.0, Decimal('100.0'), Decimal('0.01'))
    assert size2 == Decimal('0')

    # 3. Low allocation clamped to exchange minNotional (5.0 USDT)
    size3 = calculate_kelly_size(0.51, 1.0, Decimal('50.0'), Decimal('0.1'), min_notional=Decimal('5.0'))
    assert size3 == Decimal('5.0')

    # 4. Sizing with asset price (BTC at 60,000 USDT)
    btc_price = Decimal('60000.0')
    btc_step = Decimal('0.0001')
    size4 = calculate_kelly_size(0.70, 2.0, Decimal('50.0'), btc_step, min_notional=Decimal('5.0'), price=btc_price)
    assert size4 > Decimal('0')
    # Clamped to canary cap
    assert size4 * btc_price <= Decimal('50.0')
    assert size4 % btc_step == Decimal('0')


@pytest.mark.asyncio
async def test_dynamic_alpha_gate_in_maybe_schedule_spot():
    import time
    from trader.intelligence.model import LinearProbabilityModel
    from trader.intelligence.features import MICROSTRUCTURE_FEATURE_NAMES
    from trader.live.automation import maybe_schedule_spot

    # Model predicting P(Up) < 0.60
    m_low = LinearProbabilityModel((0.1,) * 15, -5.0, (0.0,) * 15, (1.0,) * 15, 0.55, feature_names=MICROSTRUCTURE_FEATURE_NAMES)
    art_low = m_low.to_artifact()

    # Model predicting P(Up) >= 0.60
    m_high = LinearProbabilityModel((0.1,) * 15, 5.0, (0.0,) * 15, (1.0,) * 15, 0.55, feature_names=MICROSTRUCTURE_FEATURE_NAMES)
    art_high = m_high.to_artifact()

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self._row = row
            self._rows = rows or []
        async def fetchone(self):
            return self._row
        async def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self, canary_artifact=None):
            self.canary_artifact = canary_artifact
            self.inserted_intents = []
            now = int(time.time() * 1000)
            self.bars = []
            base = now - 36 * 60000
            for i in range(35):
                o_ms = base + i * 60000
                c_ms = o_ms + 59999
                price = Decimal(100 + i * 2)  # ascending prices trigger SMA BUY
                self.bars.append((o_ms, c_ms, price, price + Decimal('1'), price - Decimal('1'), price, Decimal('10')))

        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def transaction(self):
            return self
        async def execute(self, q, p=None):
            if 'FROM live_accounts' in q:
                return FakeCursor(row=('READY', True, True, {'account': {'balances': [{'asset': 'BTC', 'free': '0'}]}}))
            if 'FROM live_automation' in q:
                return FakeCursor(row=('BTCUSDT', Decimal('0.001'), 2, 5, 0, 'sha'))
            if 'FROM historical_bars' in q:
                return FakeCursor(rows=self.bars)
            if 'FROM instruments' in q:
                return FakeCursor(row=('BTC', [{'filterType': 'LOT_SIZE', 'stepSize': '0.0001'}, {'filterType': 'MIN_NOTIONAL', 'minNotional': '5.0'}]))
            if 'SELECT COUNT(*) FROM live_order_intents' in q:
                return FakeCursor(row=(0,))
            if 'CANARY_ACTIVE' in q:
                if self.canary_artifact:
                    return FakeCursor(row=('canary-uuid-1', self.canary_artifact, 1))
                return FakeCursor(row=None)
            if 'FROM market_latest' in q:
                return FakeCursor(row=({'best_bid': '100', 'best_ask': '101', 'bid_qty': '10', 'ask_qty': '10'},))
            if 'INSERT INTO live_order_intents' in q:
                self.inserted_intents.append((q, p))
                return FakeCursor()
            return FakeCursor()

    class FakeRepo:
        def __init__(self, canary_artifact=None):
            self.conn = FakeConn(canary_artifact)
        class FakePool:
            def __init__(self, conn):
                self.conn = conn
            def connection(self):
                return self.conn
        @property
        def db(self):
            class DB:
                pool = self.FakePool(self.conn)
            return DB()
        async def readiness(self, conn):
            pass
        async def has_unresolved(self, conn, code):
            return False

    class FakeSettings:
        symbols = ('BTCUSDT',)
        max_order_usdt = Decimal('50')
        max_daily_orders = 10
        def configured(self, code):
            return True

    # 1. Fallback when no active canary model exists -> standard SMA BUY without attribution
    repo_none = FakeRepo(canary_artifact=None)
    res_none = await maybe_schedule_spot(repo_none, FakeSettings())
    assert res_none == 'BUY'
    assert len(repo_none.conn.inserted_intents) == 1
    intent_none = repo_none.conn.inserted_intents[0][1][2]
    assert 'ai_attribution' not in intent_none.obj

    # 2. Veto when active model predicts low win probability (P < 0.60)
    repo_low = FakeRepo(canary_artifact=art_low)
    res_low = await maybe_schedule_spot(repo_low, FakeSettings())
    assert res_low == 'vetoed_by_alpha_model'
    assert len(repo_low.conn.inserted_intents) == 0

    # 3. Approval and Kelly sizing with attribution when active model predicts P >= 0.60
    repo_high = FakeRepo(canary_artifact=art_high)
    res_high = await maybe_schedule_spot(repo_high, FakeSettings())
    assert res_high == 'BUY'
    assert len(repo_high.conn.inserted_intents) == 1
    intent_high = repo_high.conn.inserted_intents[0][1][2].obj
    assert 'ai_attribution' in intent_high
    assert intent_high['ai_attribution']['model_id'] == 'canary-uuid-1'
    assert intent_high['ai_attribution']['forecast_p'] >= 0.60
    assert len(intent_high['ai_attribution']['top_contributions']) <= 3


@pytest.mark.asyncio
async def test_get_order_attribution_in_live_repository():
    from trader.live.repository import LiveRepository

    class FakeCursor:
        def __init__(self, row=None):
            self._row = row
        async def fetchone(self):
            return self._row

    class FakeConn:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def execute(self, q, p=None):
            if "SELECT payload->'ai_attribution'" in q:
                if p[0] == 'order-with-ai':
                    return FakeCursor(row=({'model_id': 'm-123', 'forecast_p': 0.65},))
                return FakeCursor(row=(None,))
            return FakeCursor()

    class FakePool:
        def connection(self):
            return FakeConn()

    class FakeDB:
        pool = FakePool()

    repo = LiveRepository(FakeDB())
    attr = await repo.get_order_attribution('order-with-ai')
    assert attr is not None
    assert attr['model_id'] == 'm-123'
    assert attr['forecast_p'] == 0.65

    none_attr = await repo.get_order_attribution('order-without-ai')
    assert none_attr is None




