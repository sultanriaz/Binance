from decimal import Decimal as D
import pytest

from trader.paper.domain import Account, Policy, Quote, dec
from trader.paper.engine import (place_order, mark, process_quote, process_bar,
                                 apply_funding, cancel_order)
from trader.paper.risk import RiskRejected


def q(market='spot', bid='99', ask='101', qty='10000', ts=1_760_000_000_000):
    return Quote(market,'BTCUSDT',D(bid),D(ask),D(qty),D(qty),ts)


def account(cash='10000'):
    return Account.create('demo', D(cash))


def test_spot_buy_sell_cash_equity_and_ledger_sequence():
    a=account()
    policy=Policy()
    buy=place_order(a,q(),policy,side='BUY',quantity=D('2'),ts_ms=q().ts_ms,client_id='spot-buy-123')
    assert buy['status']=='FILLED'
    assert dec(a.spot['BTCUSDT']['qty'])==D('2')
    assert a.cash<D('10000')
    assert a.ledger[0]['type']=='ORDER_ACCEPTED'
    assert a.ledger[1]['type']=='FILL'
    assert a.equity()!=D('10000')
    sell=place_order(a,q(bid='110',ask='111',ts=q().ts_ms+1000),policy,
                     side='SELL',quantity=D('2'),ts_ms=q().ts_ms+1000,
                     reduce_only=True,client_id='spot-sell-123')
    assert sell['status']=='FILLED'
    assert dec(a.spot['BTCUSDT']['qty'])==0
    assert a.cash>D('10000')
    assert a.realized_pnl>0
    assert [e['seq'] for e in a.ledger]==list(range(1,a.sequence+1))


def test_idempotent_same_client_id_no_double_fill():
    a=account()
    quote=q()
    a1=place_order(a,quote,Policy(),side='BUY',quantity=D('1'),
                   ts_ms=quote.ts_ms,client_id='stable-id-0001')
    before=(a.cash,len(a.ledger),len(a.orders))
    a2=place_order(a,quote,Policy(),side='BUY',quantity=D('1'),
                   ts_ms=quote.ts_ms,client_id='stable-id-0001')
    assert a1['id']==a2['id']
    assert before==(a.cash,len(a.ledger),len(a.orders))


def test_spot_no_shorts_or_overallocation_and_stale_quote():
    a=account('1000')
    quote=q()
    with pytest.raises(RiskRejected,match='INSUFFICIENT_SPOT_ASSET'):
        place_order(a,quote,Policy(max_order_notional=D('10000'),max_position_fraction=D('1')),
                    side='SELL',quantity=D('1'),ts_ms=quote.ts_ms,client_id='short-spot')
    with pytest.raises(RiskRejected,match='STALE_QUOTE'):
        place_order(a,quote,Policy(),side='BUY',quantity=D('1'),
                    ts_ms=quote.ts_ms+20_000,client_id='stale-quote')
    with pytest.raises(RiskRejected,match='MAX_ORDER_NOTIONAL'):
        place_order(a,quote,Policy(),side='BUY',quantity=D('20'),
                    ts_ms=quote.ts_ms,client_id='too-large')


def test_limit_not_filled_until_crossed_and_partial_liquidity():
    a=account()
    p=Policy(participation_fraction=D('.20'))
    resting=place_order(a,q(),p,side='BUY',quantity=D('2'),order_type='LIMIT',
                        limit_price=D('97'),ts_ms=q().ts_ms,client_id='limit-buy')
    assert resting['status']=='OPEN'
    assert a.cash==D('10000')
    assert not process_quote(a,q(bid='96',ask='98',qty='2',ts=q().ts_ms+1000),p)
    fills=process_quote(a,q(bid='95',ask='96',qty='2',ts=q().ts_ms+2000),p)
    assert len(fills)==1
    assert resting['status']=='PARTIALLY_FILLED'
    assert dec(resting['filled'])==D('.4')
    cancelled=cancel_order(a,resting['id'],q().ts_ms+3000)
    assert cancelled['status']=='PARTIALLY_FILLED_CANCELLED'
    assert dec(a.spot['BTCUSDT']['qty'])==D('.4')


def test_stop_and_take_profit_oco_on_real_quote():
    a=account()
    p=Policy()
    place_order(a,q(),p,side='BUY',quantity=D('1'),ts_ms=q().ts_ms,client_id='start-long')
    stop=place_order(a,q(),p,side='SELL',quantity=D('1'),order_type='STOP_MARKET',
                     trigger_price=D('90'),reduce_only=True,oco_group='bracket-1',
                     ts_ms=q().ts_ms,client_id='stop-id-123')
    take=place_order(a,q(),p,side='SELL',quantity=D('1'),order_type='TAKE_PROFIT_MARKET',
                     trigger_price=D('110'),reduce_only=True,oco_group='bracket-1',
                     ts_ms=q().ts_ms,client_id='take-id-123')
    assert stop['status']=='OPEN' and take['status']=='OPEN'
    process_quote(a,q(bid='112',ask='113',ts=q().ts_ms+1000),p)
    assert take['status']=='FILLED'
    assert stop['status']=='CANCELLED'
    assert dec(a.spot['BTCUSDT']['qty'])==0


def test_futures_short_and_close_realized_and_funding():
    a=account()
    p=Policy()
    open_short=place_order(a,q('futures'),p,side='SELL',quantity=D('2'),leverage=3,
                           ts_ms=q().ts_ms,client_id='open-short')
    assert open_short['status']=='FILLED'
    assert dec(a.futures['BTCUSDT']['qty'])==D('-2')
    cash=a.cash
    amount=apply_funding(a,'BTCUSDT',D('.0001'),q().ts_ms+100)
    assert amount>0 and a.cash==cash+amount
    close=place_order(a,q('futures',bid='90',ask='91',ts=q().ts_ms+1000),p,
                      side='BUY',quantity=D('2'),reduce_only=True,
                      ts_ms=q().ts_ms+1000,client_id='close-short')
    assert close['status']=='FILLED'
    assert dec(a.futures['BTCUSDT']['qty'])==0
    assert a.realized_pnl>0
    assert a.futures['BTCUSDT']['margin']=='0'


def test_futures_reversal_requires_reduction():
    a=account()
    p=Policy()
    place_order(a,q('futures'),p,side='BUY',quantity=D('1'),leverage=2,
                ts_ms=q().ts_ms,client_id='long-start')
    with pytest.raises(RiskRejected,match='FUTURES_REVERSAL_REQUIRES_REDUCE_ONLY'):
        place_order(a,q('futures'),p,side='SELL',quantity=D('1'),leverage=2,
                    ts_ms=q().ts_ms,client_id='bad-short')


def test_futures_liquidation_mark_closes_position():
    a=account()
    p=Policy()
    place_order(a,q('futures',bid='99',ask='101'),p,side='BUY',quantity=D('2'),leverage=3,
                ts_ms=q().ts_ms,client_id='liq-long')
    mark(a,q('futures',bid='60',ask='60',ts=q().ts_ms+1000),p)
    assert dec(a.futures['BTCUSDT']['qty'])==0
    assert any(e['type']=='SIMULATED_LIQUIDATION' for e in a.ledger)


def test_drawdown_pauses_new_trades_but_allows_exit():
    a=account()
    p=Policy(max_total_drawdown_fraction=D('.01'))
    place_order(a,q(),p,side='BUY',quantity=D('2'),ts_ms=q().ts_ms,client_id='drop-buy')
    mark(a,q(bid='20',ask='20',ts=q().ts_ms+1000),p)
    assert a.paused
    with pytest.raises(RiskRejected,match='TRADING_PAUSED'):
        place_order(a,q(bid='20',ask='20',ts=q().ts_ms+1000),p,
                    side='BUY',quantity=D('1'),ts_ms=q().ts_ms+1000,client_id='paused-entry')
    result=place_order(a,q(bid='20',ask='20',ts=q().ts_ms+1000),p,
                       side='SELL',quantity=D('2'),reduce_only=True,
                       ts_ms=q().ts_ms+1000,client_id='paused-exit')
    assert result['status']=='FILLED'


def test_serialization_roundtrip_exact_decimals():
    a=account()
    place_order(a,q(),Policy(),side='BUY',quantity=D('.12345678'),
                            ts_ms=q().ts_ms,client_id='serialization')
    b=Account.from_dict(a.to_dict())
    assert b.to_dict()==a.to_dict()
    assert b.equity()==a.equity()


def test_bar_replay_protective_order_does_not_fill_same_bar_created():
    a=account()
    p=Policy()
    place_order(a,q(),p,side='BUY',quantity=D('1'),ts_ms=q().ts_ms,client_id='before-bar')
    stop=place_order(a,q(),p,side='SELL',quantity=D('1'),reduce_only=True,
                     order_type='STOP_MARKET',trigger_price=D('90'),
                     ts_ms=q().ts_ms,client_id='stop-bar')
    process_bar(a,market='spot',symbol='BTCUSDT',open_price=D('100'),high=D('110'),
                low=D('80'),close=D('90'),volume=D('1000'),ts_ms=q().ts_ms,policy=p)
    assert stop['status']=='OPEN'
    process_bar(a,market='spot',symbol='BTCUSDT',open_price=D('100'),high=D('110'),
                low=D('80'),close=D('90'),volume=D('1000'),ts_ms=q().ts_ms+60000,policy=p)
    assert stop['status']=='FILLED'


def test_spot_sell_without_explicit_reduce_only_is_not_a_short():
    a=account()
    p=Policy()
    place_order(a,q(),p,side='BUY',quantity=D('1'),ts_ms=q().ts_ms,client_id='buy-normal')
    result=place_order(a,q(),p,side='SELL',quantity=D('1'),ts_ms=q().ts_ms,
                       client_id='sell-normal',reduce_only=False)
    assert result['status']=='FILLED'
    assert dec(a.spot['BTCUSDT']['qty'])==0


def test_reused_order_key_with_modified_parameters_is_rejected():
    a=account()
    p=Policy()
    place_order(a,q(),p,side='BUY',quantity=D('1'),ts_ms=q().ts_ms,client_id='duplicate-key')
    with pytest.raises(RiskRejected,match='CLIENT_ID_REUSE_CONFLICT'):
        place_order(a,q(),p,side='BUY',quantity=D('2'),ts_ms=q().ts_ms,client_id='duplicate-key')


def test_pyramiding_checks_total_symbol_position():
    a=account()
    p=Policy(max_order_notional=D('10000'))
    place_order(a,q(),p,side='BUY',quantity=D('20'),ts_ms=q().ts_ms,client_id='first-layer')
    with pytest.raises(RiskRejected,match='MAX_POSITION_FRACTION'):
        place_order(a,q(),p,side='BUY',quantity=D('15'),ts_ms=q().ts_ms,client_id='second-layer')


def test_drawdown_pause_cancels_pending_opening_limit():
    a=account()
    p=Policy(max_total_drawdown_fraction=D('.01'))
    place_order(a,q(),p,side='BUY',quantity=D('2'),ts_ms=q().ts_ms,
                client_id='owned-before-loss')
    pending=place_order(a,q(),p,side='BUY',quantity=D('2'),order_type='LIMIT',
                        limit_price=D('50'),ts_ms=q().ts_ms,client_id='pending-before-loss')
    assert pending['status']=='OPEN'
    mark(a,q(bid='30',ask='30',ts=q().ts_ms+1000),p)
    assert a.paused
    assert pending['status']=='CANCELLED'
    assert any(e['type']=='RISK_CANCELLED_OPENING_ORDER' for e in a.ledger)
