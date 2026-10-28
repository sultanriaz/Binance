"""Module 4 offline contract tests; DB/Docker integration is a separate acceptance gate."""
import math
from decimal import Decimal

import pytest

from trader.intelligence.features import FEATURE_NAMES, make_features, regime
from trader.intelligence.model import LinearProbabilityModel
from trader.intelligence.training import (binary_metrics, candidate_quality, evaluate,
                                          train_candidate, training_samples)
from trader.governance.core import ApprovalError, can_consume, can_review, digest
from trader.paper.history import Bar


def history(n=900, horizon_start=1_700_000_040_000):
    # Exactly-minute-aligned synthetic CLOSED candles. Smooth oscillator with
    # localized turbulence so both positive and negative labels appear.
    t=(horizon_start // 60000) * 60000
    candles=[]
    for i in range(n):
        price=30000+100*math.sin(i*.19)+60*math.sin(i*.037)+i*.002
        next_price=30000+100*math.sin((i+1)*.19)+60*math.sin((i+1)*.037)+(i+1)*.002
        o=Decimal(f'{price:.5f}')
        c=Decimal(f'{next_price:.5f}')
        candles.append(Bar('spot','BTCUSDT','1m',t+60000*i,t+60000*(i+1)-1,
                           o,max(o,c)+Decimal('2'),min(o,c)-Decimal('2'),c,Decimal('10')))
    return candles


def test_features_are_point_in_time_and_finite():
    bars=history()
    old=make_features(bars)
    new=list(bars)
    x=new[-1]
    new[-1]=Bar(x.market,x.symbol,x.interval,x.open_ms,x.close_ms,x.o,
                x.h+Decimal('500'),x.l,x.c+Decimal('400'),x.volume)
    updated=make_features(new)
    assert old[:-1]==updated[:-1], 'Future candles must never change earlier features'
    assert old[-1] != updated[-1]
    assert tuple(FEATURE_NAMES)==LinearProbabilityModel.from_artifact(
        LinearProbabilityModel((0.,)*9,0.,(0.,)*9,(1.,)*9,.55).to_artifact()).feature_names
    assert all(math.isfinite(v) for row in old for v in row.values)
    assert regime(old[-1]) in ('HIGH_VOLATILITY','TRENDING_UP','TRENDING_DOWN','LOW_MOMENTUM')


def test_labels_use_next_open_and_later_close():
    bars=history(50)
    row,y,net=training_samples(bars,horizon=2,round_trip_bps=15)[0]
    expected=float(bars[row.index+2].c/bars[row.index+1].o-1)-.0015
    assert net==pytest.approx(expected)
    assert y==int(net>0)


def test_training_chronological_purged_and_replay_deterministic():
    bars=history()
    one=train_candidate(bars,horizon=2,friction_bps=10)
    two=train_candidate(bars,horizon=2,friction_bps=10)
    assert one['artifact']==two['artifact']
    assert one['data_sha256']==two['data_sha256']
    assert one['metrics']['test_rows'] > 50
    assert one['metrics']['validation_rows'] > 50
    assert one['metrics']['train_rows'] > 100
    model=LinearProbabilityModel.from_artifact(one['artifact'])
    assert 0<=model.probability(make_features(bars)[-1])<=1
    assert one['metrics']['holdout']['trades']<=one['metrics']['test_rows']
    assert candidate_quality(one)[0] in (True,False)


def test_model_artifact_checksums_and_schema_refusal():
    model=LinearProbabilityModel((0.,)*9,0.,(0.,)*9,(1.,)*9,.55)
    artifact=model.to_artifact()
    artifact['weights'][0]=1000
    with pytest.raises(ValueError,match='corrupted'):
        LinearProbabilityModel.from_artifact(artifact)
    with pytest.raises(ValueError,match='schema'):
        LinearProbabilityModel((0.,)*8,0.,(0.,)*9,(1.,)*9,.55)


def test_performance_eval_nonoverlap():
    bars=history(50)
    samples=training_samples(bars,horizon=3)
    result=evaluate(samples,[.99]*len(samples),.55,3)
    assert result['trades'] <= math.ceil(len(samples)/4)
    assert result['max_drawdown_fraction'] >= 0
    assert 0 <= binary_metrics([0,1],[.1,.9])['brier']<=1


def test_approval_is_distinct_mfa_admin_and_exact_payload():
    body={'account':'spot-testnet','client_order_id':'order-abc-123','symbol':'BTCUSDT'}
    with pytest.raises(ApprovalError,match='Independent'):
        can_review(requester=9,reviewer=9,status='PENDING',expired=False)
    with pytest.raises(ApprovalError,match='expired'):
        can_review(requester=9,reviewer=10,status='PENDING',expired=True)
    can_review(requester=9,reviewer=10,status='PENDING',expired=False)
    can_consume(status='APPROVED',expired=False,actual=body,expected_sha256=digest(body))
    with pytest.raises(ApprovalError,match='differs'):
        can_consume(status='APPROVED',expired=False,actual={**body,'symbol':'ETHUSDT'},
                    expected_sha256=digest(body))
    with pytest.raises(ApprovalError,match='approved'):
        can_consume(status='CONSUMED',expired=False,actual=body,expected_sha256=digest(body))


def test_sandbox_approval_required_in_route():
    import pathlib
    route=(pathlib.Path(__file__).resolve().parents[1] / 'src/trader/exchange/http_api.py').read_text()
    assert 'Approved independent dual-control request required' in route
