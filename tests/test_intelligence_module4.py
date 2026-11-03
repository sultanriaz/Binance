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


def test_microstructure_features_point_in_time_and_finite():
    from trader.intelligence.features import (
        MICROSTRUCTURE_FEATURE_NAMES,
        extract_microstructure_context,
        make_features,
    )

    bars = history(100)
    # 1. Verify extraction with live context
    ctx = extract_microstructure_context(
        book_ticker_payload={'bid_qty': '10.5', 'ask_qty': '5.5'},
        mark_price_payload={'funding_rate': '0.00015'}
    )
    assert ctx['book_imbalance'] == pytest.approx((10.5 - 5.5) / 16.0)
    assert ctx['funding_rate'] == pytest.approx(0.00015)

    # 2. Generate 15 microstructure features
    rows = make_features(bars, context=ctx, feature_names=MICROSTRUCTURE_FEATURE_NAMES)
    assert len(rows) > 0
    assert len(rows[0].values) == 15
    assert len(MICROSTRUCTURE_FEATURE_NAMES) == 15

    # 3. Verify all features are strictly finite
    for r in rows:
        assert all(math.isfinite(v) for v in r.values)

    # 4. Point-in-time test: altering future bar must not alter past features
    bars_mod = list(bars)
    last = bars_mod[-1]
    bars_mod[-1] = Bar(last.market, last.symbol, last.interval, last.open_ms, last.close_ms,
                       last.o, last.h + Decimal('200'), last.l - Decimal('100'), last.c, last.volume * Decimal('5'))
    rows_mod = make_features(bars_mod, feature_names=MICROSTRUCTURE_FEATURE_NAMES)
    assert [r.values for r in rows[:-1]] == [r.values for r in rows_mod[:-1]]
    assert rows[-1].values != rows_mod[-1].values


def test_microstructure_model_training_and_serialization():
    from trader.intelligence.features import MICROSTRUCTURE_FEATURE_NAMES, make_features
    from trader.intelligence.training import train_candidate

    bars = history(800)
    result = train_candidate(bars, horizon=2, friction_bps=10, feature_names=MICROSTRUCTURE_FEATURE_NAMES)

    artifact = result['artifact']
    assert artifact['feature_names'] == list(MICROSTRUCTURE_FEATURE_NAMES)
    assert len(artifact['weights']) == 15
    assert len(artifact['means']) == 15
    assert len(artifact['scales']) == 15

    model = LinearProbabilityModel.from_artifact(artifact)
    assert model.feature_names == MICROSTRUCTURE_FEATURE_NAMES

    # Run inference on latest bar
    row = make_features(bars, feature_names=MICROSTRUCTURE_FEATURE_NAMES)[-1]
    prob = model.probability(row)
    assert 0.0 <= prob <= 1.0


def test_dual_schema_coexistence():
    from trader.intelligence.features import LEGACY_FEATURE_NAMES, MICROSTRUCTURE_FEATURE_NAMES

    # 9-feature legacy model
    m9 = LinearProbabilityModel((0.1,) * 9, 0.0, (0.0,) * 9, (1.0,) * 9, 0.55, feature_names=LEGACY_FEATURE_NAMES)
    art9 = m9.to_artifact()
    loaded9 = LinearProbabilityModel.from_artifact(art9)
    assert loaded9.feature_names == LEGACY_FEATURE_NAMES

    # 15-feature microstructure model
    m15 = LinearProbabilityModel((0.1,) * 15, 0.0, (0.0,) * 15, (1.0,) * 15, 0.55, feature_names=MICROSTRUCTURE_FEATURE_NAMES)
    art15 = m15.to_artifact()
    loaded15 = LinearProbabilityModel.from_artifact(art15)
    assert loaded15.feature_names == MICROSTRUCTURE_FEATURE_NAMES


def test_cpcv_splits_purging_and_embargoing():
    from trader.intelligence.validation import cpcv_splits

    n_samples = 200
    n_groups = 5
    k_test = 2
    horizon = 3
    embargo_pct = 0.02
    expected_embargo = max(horizon, int(n_samples * embargo_pct))  # 4

    splits = cpcv_splits(n_samples, n_groups=n_groups, k_test_groups=k_test,
                         horizon=horizon, embargo_pct=embargo_pct)
    # C(5, 2) = 10 combinatorial splits
    assert len(splits) == 10

    for train_idx, test_idx in splits:
        # Invariant 1: Disjoint train and test partitions
        assert set(train_idx).isdisjoint(set(test_idx))

        # Group boundaries
        group_size = n_samples // n_groups
        # Reconstruct test blocks
        test_blocks = []
        for g in range(n_groups):
            g_start = g * group_size
            g_end = n_samples if g == n_groups - 1 else (g + 1) * group_size
            if g_start in test_idx:
                test_blocks.append((g_start, g_end))

        # Invariant 2: Purging - no train label overlaps into any test block
        for tr in train_idx:
            for t_start, t_end in test_blocks:
                if tr < t_start:
                    assert tr + horizon < t_start, f"Purging violation: train {tr} + {horizon} >= test start {t_start}"
                if tr >= t_end:
                    # Invariant 3: Embargoing - post-test buffer dropped
                    assert tr >= t_end + expected_embargo, f"Embargo violation: train {tr} < {t_end + expected_embargo}"

    # Error handling on invalid parameters
    with pytest.raises(ValueError, match="minimum 50 required"):
        cpcv_splits(40)
    with pytest.raises(ValueError, match="Invalid group parameters"):
        cpcv_splits(100, n_groups=2, k_test_groups=2)


def test_deflated_sharpe_ratio_and_p_value():
    from trader.intelligence.validation import deflated_sharpe_ratio

    # 1. Genuine alpha: High Sharpe (2.5), low trial variance (0.02), small number of trials (5)
    dsr_high = deflated_sharpe_ratio(
        estimated_sharpe=2.5, trials=5, variance_sharpe=0.02,
        skewness=-0.2, kurtosis=3.5, sample_length=500
    )
    assert dsr_high.dsr_stat > 2.0
    assert dsr_high.p_value < 0.05
    assert isinstance(dsr_high, float)
    # Test unpacking
    stat, pval = dsr_high
    assert stat == dsr_high.dsr_stat
    assert pval == dsr_high.p_value

    # 2. False discovery (data snooping): Low Sharpe (0.2), 1000 trials, high variance (0.50)
    dsr_snoop = deflated_sharpe_ratio(
        estimated_sharpe=0.2, trials=1000, variance_sharpe=0.50,
        skewness=0.0, kurtosis=3.0, sample_length=100
    )
    assert dsr_snoop.dsr_stat < 0.0
    assert dsr_snoop.p_value > 0.50  # Strongly rejected by DSR gate


def test_probability_of_backtest_overfitting_pbo():
    from trader.intelligence.validation import probability_of_backtest_overfitting

    # 1. Consistent strategy: Strategy 0 is always best in-sample and out-of-sample
    is_matrix = [[1.5, 0.5, 0.2] for _ in range(10)]
    oos_matrix = [[1.2, 0.4, 0.1] for _ in range(10)]
    pbo_clean = probability_of_backtest_overfitting(is_matrix, oos_matrix)
    assert pbo_clean == 0.0

    # 2. Overfitted strategy: In-sample best is consistently worst out-of-sample
    is_overfit = [[2.0, 0.5, 0.4] for _ in range(10)]
    oos_overfit = [[-0.5, 0.8, 0.7] for _ in range(10)]
    pbo_bad = probability_of_backtest_overfitting(is_overfit, oos_overfit)
    assert pbo_bad == 1.0

    # 3. Path-based single argument call
    paths = [([1.5, 0.5, 0.2], [1.2, 0.4, 0.1]) for _ in range(10)]
    pbo_paths = probability_of_backtest_overfitting(paths)
    assert pbo_paths == 0.0


def test_candidate_quality_gate_hardening():
    # Base valid metrics payload
    base_candidate = {
        'metrics': {
            'brier': 0.20,
            'baseline_brier': 0.25,
            'log_loss': 0.58,
            'baseline_log_loss': 0.69,
            'holdout': {
                'trades': 15,
                'net_return_fraction': 0.045,
                'max_drawdown_fraction': 0.03
            },
            'cpcv': {
                'pbo': 0.08,
                'dsr_p_value': 0.015,
                'min_test_net_return': 0.012
            }
        }
    }

    # 1. Healthy model passing all hardened gates
    passed, reasons = candidate_quality(base_candidate)
    assert passed is True
    assert reasons == []

    # 2. Fails on PBO >= 0.15
    bad_pbo = {
        'metrics': {
            **base_candidate['metrics'],
            'cpcv': {**base_candidate['metrics']['cpcv'], 'pbo': 0.22}
        }
    }
    passed, reasons = candidate_quality(bad_pbo)
    assert passed is False
    assert any('PBO' in r for r in reasons)

    # 3. Fails on DSR p-value >= 0.05
    bad_dsr = {
        'metrics': {
            **base_candidate['metrics'],
            'cpcv': {**base_candidate['metrics']['cpcv'], 'dsr_p_value': 0.08}
        }
    }
    passed, reasons = candidate_quality(bad_dsr)
    assert passed is False
    assert any('Deflated Sharpe Ratio' in r for r in reasons)

    # 4. Fails on nonpositive combinatorial test path return
    bad_path = {
        'metrics': {
            **base_candidate['metrics'],
            'cpcv': {**base_candidate['metrics']['cpcv'], 'min_test_net_return': -0.005}
        }
    }
    passed, reasons = candidate_quality(bad_path)
    assert passed is False
    assert any('combinatorial test paths' in r for r in reasons)

    # 5. Backward compatibility: Legacy model without 'cpcv' key evaluates holdout metrics cleanly
    legacy_candidate = {
        'metrics': {
            'brier': 0.21,
            'baseline_brier': 0.25,
            'log_loss': 0.59,
            'baseline_log_loss': 0.69,
            'holdout': {
                'trades': 12,
                'net_return_fraction': 0.03,
                'max_drawdown_fraction': 0.04
            }
        }
    }
    passed, reasons = candidate_quality(legacy_candidate)
    assert passed is True
    assert reasons == []


def test_end_to_end_training_with_cpcv():
    bars = history(400)
    result = train_candidate(bars, horizon=2, friction_bps=10)

    # Verify CPCV metrics are attached
    assert 'cpcv' in result['metrics']
    cpcv = result['metrics']['cpcv']
    assert 0.0 <= cpcv['pbo'] <= 1.0
    assert 0.0 <= cpcv['dsr_p_value'] <= 1.0
    assert cpcv['n_splits'] == 10
    assert 'path_net_returns' in cpcv
    assert len(cpcv['path_net_returns']) == 10
    assert isinstance(cpcv['cpcv_pass'], bool)

    # Limitations documentation
    assert any('Combinatorial Purged Cross-Validation' in limit for limit in result['limitations'])


def test_lifecycle_schema_migration_file():
    import pathlib
    migration_file = pathlib.Path(__file__).resolve().parents[1] / 'infrastructure/migrations/007_lifecycle.sql'
    assert migration_file.exists(), '007_lifecycle.sql migration file must exist'
    sql = migration_file.read_text()
    assert 'research_models_status_check' in sql
    for st in ('CANDIDATE', 'SHADOW', 'CANARY_ACTIVE', 'DECAYING', 'RETIRED'):
        assert f"'{st}'" in sql
    assert 'shadow_samples_count' in sql
    assert 'rolling_brier' in sql
    assert 'realized_win_rate' in sql
    assert 'research_canary_unique' in sql
    assert "'007_lifecycle'" in sql


def test_lifecycle_benchmark_thresholds():
    # Helper to test lifecycle decision logic
    def evaluate_lifecycle(count: int, brier: float, win_rate: float, cum_ret: float, drawdown: float, current_status: str):
        if count < 100:
            return current_status  # No transition before 100 samples
        if current_status == 'SHADOW':
            if brier <= 0.22 and win_rate >= 0.55 and cum_ret > 0:
                return 'CANARY_ACTIVE'
            elif brier > 0.26 or drawdown > 0.08:
                return 'RETIRED'
            return 'SHADOW'
        elif current_status == 'CANARY_ACTIVE':
            if brier > 0.26 or drawdown > 0.08:
                return 'RETIRED'
            return 'CANARY_ACTIVE'
        return current_status

    # 1. Not enough samples (< 100) -> stays in SHADOW
    assert evaluate_lifecycle(count=80, brier=0.20, win_rate=0.60, cum_ret=0.05, drawdown=0.02, current_status='SHADOW') == 'SHADOW'

    # 2. Meets promotion gate (count >= 100, brier <= 0.22, win_rate >= 0.55, cum_ret > 0) -> CANARY_ACTIVE
    assert evaluate_lifecycle(count=120, brier=0.21, win_rate=0.58, cum_ret=0.04, drawdown=0.03, current_status='SHADOW') == 'CANARY_ACTIVE'

    # 3. Fails promotion: Win rate below 0.55 -> stays in SHADOW
    assert evaluate_lifecycle(count=120, brier=0.21, win_rate=0.52, cum_ret=0.01, drawdown=0.03, current_status='SHADOW') == 'SHADOW'

    # 4. Fails promotion: Negative cumulative return -> stays in SHADOW
    assert evaluate_lifecycle(count=120, brier=0.21, win_rate=0.56, cum_ret=-0.02, drawdown=0.04, current_status='SHADOW') == 'SHADOW'

    # 5. Decay retirement: Brier exceeds 0.26 -> RETIRED
    assert evaluate_lifecycle(count=120, brier=0.28, win_rate=0.50, cum_ret=-0.01, drawdown=0.04, current_status='SHADOW') == 'RETIRED'

    # 6. Drawdown retirement: Drawdown exceeds 8% (0.08) -> RETIRED
    assert evaluate_lifecycle(count=120, brier=0.23, win_rate=0.54, cum_ret=0.02, drawdown=0.09, current_status='SHADOW') == 'RETIRED'

    # 7. Canary model retires when degrading
    assert evaluate_lifecycle(count=200, brier=0.27, win_rate=0.48, cum_ret=0.00, drawdown=0.05, current_status='CANARY_ACTIVE') == 'RETIRED'


@pytest.mark.asyncio
async def test_repository_lifecycle_state_machine_transitions():
    from trader.intelligence.repository import ResearchRepo

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self._row = row
            self._rows = rows or []
        async def fetchone(self):
            return self._row
        async def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self, state):
            self.state = state
            self.executed = []
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def transaction(self):
            return self
        async def execute(self, q, p=None):
            self.executed.append((q, p))
            if 'WHERE id=%s FOR UPDATE' in q:
                m = self.state.get(p[0])
                if m:
                    return FakeCursor(row=(m['market'], m['symbol'], m['interval'], m['status']))
                return FakeCursor(row=None)
            if "WHERE status='CANARY_ACTIVE'" in q:
                for mid, m in self.state.items():
                    if m.get('status') == 'CANARY_ACTIVE':
                        return FakeCursor(row=(
                            mid, m['market'], m['symbol'], m['interval'], 1, 1000,
                            {}, {}, True, 'CANARY_ACTIVE', None, None,
                            m.get('shadow_samples_count', 120), m.get('rolling_brier', 0.20), m.get('realized_win_rate', 0.60)
                        ))
                return FakeCursor(row=None)
            if 'SET status=\'CANARY_ACTIVE\'' in q:
                model_id = p[3]
                self.state[model_id]['status'] = 'CANARY_ACTIVE'
                return FakeCursor()
            if "UPDATE research_models" in q and "status='RETIRED'" in q:
                if 'WHERE id=%s' in q:
                    model_id = p[-1]
                    if model_id in self.state:
                        self.state[model_id]['status'] = 'RETIRED'
                elif 'WHERE market=%s' in q:
                    for mid, m in self.state.items():
                        if m['market'] == p[0] and m['symbol'] == p[1] and m['interval'] == p[2] and m['status'] == 'CANARY_ACTIVE':
                            m['status'] = 'RETIRED'
                return FakeCursor()
            if 'SET shadow_samples_count=%s' in q:
                mid = p[3]
                if mid in self.state:
                    self.state[mid]['shadow_samples_count'] = p[0]
                    self.state[mid]['rolling_brier'] = p[1]
                    self.state[mid]['realized_win_rate'] = p[2]
                return FakeCursor()
            return FakeCursor()

    class FakePool:
        def __init__(self, state):
            self.state = state
            self.conn = FakeConn(state)
        def connection(self):
            return self.conn

    class FakeDB:
        def __init__(self, state):
            self.pool = FakePool(state)
            self.audits = []
        async def audit(self, actor, action, outcome, meta=None):
            self.audits.append((actor, action, outcome, meta))

    # Initial state with candidate, shadow, and previous canary
    state = {
        'canary-0': {'market': 'spot', 'symbol': 'BTCUSDT', 'interval': '1m', 'status': 'CANARY_ACTIVE'},
        'shadow-1': {'market': 'spot', 'symbol': 'BTCUSDT', 'interval': '1m', 'status': 'SHADOW'},
        'cand-2': {'market': 'spot', 'symbol': 'BTCUSDT', 'interval': '1m', 'status': 'CANDIDATE'},
    }
    db = FakeDB(state)
    repo = ResearchRepo(db)

    # 1. Candidate cannot be promoted to CANARY_ACTIVE directly
    promoted_cand = await repo.promote_to_canary('cand-2', 120, 0.20, 0.60)
    assert promoted_cand is False
    assert state['cand-2']['status'] == 'CANDIDATE'

    # 2. Promote shadow-1 to CANARY_ACTIVE
    promoted = await repo.promote_to_canary('shadow-1', 120, 0.20, 0.60)
    assert promoted is True
    assert state['shadow-1']['status'] == 'CANARY_ACTIVE'
    # Old canary-0 is automatically demoted to RETIRED
    assert state['canary-0']['status'] == 'RETIRED'
    # Governance audit event recorded
    assert any(a[1] == 'MODEL_AUTO_PROMOTED_CANARY' for a in db.audits)

    # 3. get_active_canary returns new canary and evaluates health
    active = await repo.get_active_canary('spot', 'BTCUSDT')
    assert active is not None
    assert active['id'] == 'shadow-1'
    assert active['win_rate_health'] == 'HEALTHY'

    # 4. Auto-retire model on performance decay
    retired = await repo.auto_retire_model('shadow-1', 'drawdown_exceeded', 150, 0.28, 0.45)
    assert retired is True
    assert state['shadow-1']['status'] == 'RETIRED'
    assert any(a[1] == 'MODEL_AUTO_RETIRED_PERFORMANCE' for a in db.audits)

    # 5. Demote model by admin
    state['shadow-2'] = {'market': 'spot', 'symbol': 'ETHUSDT', 'interval': '1m', 'status': 'SHADOW'}
    demoted = await repo.demote_model('shadow-2', actor='admin_user_42')
    assert demoted is True
    assert state['shadow-2']['status'] == 'RETIRED'
    assert any(a[1] == 'research.model.demote' for a in db.audits)


@pytest.mark.asyncio
async def test_http_api_active_and_demote_routes():
    from types import SimpleNamespace
    import uuid
    import httpx
    pytest.importorskip("redis")
    from trader.api import app
    from trader.security import issue_token

    key = "test-jwt-secret-for-intelligence-module-32-chars"
    app.state.config = SimpleNamespace(jwt_secret=key)
    valid_mid = str(uuid.uuid4())

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self._row = row
            self._rows = rows or []
        async def fetchone(self):
            return self._row
        async def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self):
            self.state = {
                valid_mid: {
                    'market': 'spot', 'symbol': 'BTCUSDT', 'interval': '1m', 'horizon': 1,
                    'status': 'CANARY_ACTIVE', 'shadow_samples_count': 115,
                    'rolling_brier': 0.198, 'realized_win_rate': 0.62
                }
            }
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def transaction(self):
            return self
        async def execute(self, q, p=None):
            if "WHERE status='CANARY_ACTIVE'" in q:
                return FakeCursor(row=(
                    valid_mid, 'spot', 'BTCUSDT', '1m', 1, 1000,
                    {}, {'brier': 0.20}, True, 'CANARY_ACTIVE', None, None,
                    115, 0.198, 0.62
                ))
            if 'WHERE id=%s FOR UPDATE' in q:
                mid = str(p[0])
                m = self.state.get(mid)
                if m:
                    return FakeCursor(row=(m['market'], m['symbol'], m['interval'], m['status']))
                return FakeCursor(row=None)
            if 'WHERE id=%s' in q and 'FROM research_models' in q:
                mid = str(p[0])
                m = self.state.get(mid)
                if m:
                    return FakeCursor(row=(
                        mid, m['market'], m['symbol'], m['interval'], m.get('horizon', 1),
                        1000, {}, m['status'], True, m.get('shadow_samples_count', 0),
                        m.get('rolling_brier', 0.2), m.get('realized_win_rate', 0.5), None, {}
                    ))
                return FakeCursor(row=None)
            if 'SET status=\'RETIRED\' WHERE id=%s' in q:
                mid = str(p[-1])
                if mid in self.state:
                    self.state[mid]['status'] = 'RETIRED'
                return FakeCursor()
            return FakeCursor()

    class FakePool:
        def __init__(self):
            self.conn = FakeConn()
        def connection(self):
            return self.conn

    class FakeDB:
        def __init__(self):
            self.pool = FakePool()
            self.audits = []
        async def audit(self, actor, action, outcome, meta=None):
            self.audits.append((actor, action, outcome, meta))
        async def user_by_id(self, user_id):
            return (user_id, 'ADMIN', True) if user_id == 1 else (user_id, 'TRADER', True)

    app.state.db = FakeDB()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Unauthorized check
        r = await client.get("/v1/research/models/active")
        assert r.status_code == 401

        # Authorized Admin
        admin_token = issue_token(1, "ADMIN", key)
        headers = {"Authorization": f"Bearer {admin_token}"}

        # GET /v1/research/models/active
        r_active = await client.get("/v1/research/models/active", headers=headers)
        assert r_active.status_code == 200
        data = r_active.json()
        assert data['active_model'] is not None
        assert data['active_model']['id'] == valid_mid
        assert data['active_model']['status'] == 'CANARY_ACTIVE'
        assert data['active_model']['win_rate_health'] == 'HEALTHY'

        # POST /v1/research/models/{model_id}/demote
        r_demote = await client.post(f"/v1/research/models/{valid_mid}/demote", headers=headers)
        assert r_demote.status_code == 200
        demote_data = r_demote.json()
        assert demote_data['model_id'] == valid_mid
        assert demote_data['status'] == 'RETIRED'


def test_two_sample_ks_test():
    import random
    from trader.intelligence.drift import two_sample_ks_test

    random.seed(42)
    # 1. Samples drawn from identical distribution: high p-value (fails to reject H0)
    s1 = [random.gauss(0.0, 1.0) for _ in range(100)]
    s2 = [random.gauss(0.0, 1.0) for _ in range(50)]
    pval_identical = two_sample_ks_test(s1, s2)
    assert pval_identical > 0.05

    # 2. Shifted distribution (mean shift): p-value < 0.01 rejects H0 (concept drift)
    s_drift = [random.gauss(2.5, 1.0) for _ in range(50)]
    pval_drift = two_sample_ks_test(s1, s_drift)
    assert pval_drift < 0.01

    # 3. Small sample safety fallback
    assert two_sample_ks_test([1.0, 2.0], [3.0, 4.0]) == 1.0


def test_page_hinkley_drift_detector():
    from trader.intelligence.drift import page_hinkley_drift

    # 1. Stable low error sequence (no drift)
    stable_errors = [0.15 + (i % 3) * 0.01 for i in range(50)]
    assert page_hinkley_drift(stable_errors, threshold=5.0) is False

    # 2. Sudden error spike from 0.15 to 0.90 for 15 bars -> triggers change-point
    shock_errors = [0.15] * 35 + [0.90] * 15
    assert page_hinkley_drift(shock_errors, threshold=5.0) is True

    # 3. Short sample handling
    assert page_hinkley_drift([0.1, 0.2]) is False


def test_evaluate_model_drift():
    from trader.intelligence.drift import evaluate_model_drift
    from trader.intelligence.features import MICROSTRUCTURE_FEATURE_NAMES

    # Normal baseline reference matrix: 100 rows, 15 features
    ref_matrix = [[0.0] * 15 for _ in range(100)]
    cur_matrix_stable = [[0.0] * 15 for _ in range(50)]
    errors_stable = [0.15] * 50

    # 1. No drift when distributions and errors are stable
    drift, reasons = evaluate_model_drift(
        ref_matrix, cur_matrix_stable, errors_stable, MICROSTRUCTURE_FEATURE_NAMES
    )
    assert drift is False
    assert reasons == []

    # 2. KS Drift: 4 features shift dramatically
    cur_matrix_shifted = [[10.0 if col in (0, 1, 2, 3) else 0.0 for col in range(15)] for _ in range(50)]
    drift_ks, reasons_ks = evaluate_model_drift(
        ref_matrix, cur_matrix_shifted, errors_stable, MICROSTRUCTURE_FEATURE_NAMES
    )
    assert drift_ks is True
    assert any('Kolmogorov-Smirnov' in r for r in reasons_ks)

    # 3. Page-Hinkley Drift: errors surge past threshold
    shock_errors = [0.15] * 35 + [0.95] * 15
    drift_ph, reasons_ph = evaluate_model_drift(
        ref_matrix, cur_matrix_stable, shock_errors, MICROSTRUCTURE_FEATURE_NAMES
    )
    assert drift_ph is True
    assert any('Page-Hinkley' in r for r in reasons_ph)


@pytest.mark.asyncio
async def test_concept_drift_watchdog_and_circuit_breaker_simulation():
    import uuid
    from decimal import Decimal
    from trader.intelligence.worker import check_concept_drift
    from trader.intelligence.model import LinearProbabilityModel
    from trader.intelligence.features import MICROSTRUCTURE_FEATURE_NAMES
    from trader.paper.history import Bar

    model_id = str(uuid.uuid4())
    m = LinearProbabilityModel((0.1,) * 15, 0.0, (0.0,) * 15, (1.0,) * 15, 0.55, feature_names=MICROSTRUCTURE_FEATURE_NAMES)
    art = m.to_artifact()

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self._row = row
            self._rows = rows or []
        async def fetchone(self):
            return self._row
        async def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self):
            self.model_status = 'CANARY_ACTIVE'
            self.live_incidents = []
            self.queued_jobs = []
            self.audits = []

        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def transaction(self):
            return self

        async def execute(self, q, p=None):
            if "WHERE status='CANARY_ACTIVE'" in q:
                if self.model_status == 'CANARY_ACTIVE':
                    return FakeCursor(rows=[(model_id, 'spot', 'BTCUSDT', '1m', 1, 1000000, art)])
                return FakeCursor(rows=[])
            if 'FROM market_latest' in q:
                return FakeCursor(row=({'best_bid': '100', 'best_ask': '101', 'bid_qty': '10', 'ask_qty': '10'},))
            if 'FROM shadow_predictions' in q:
                # Prediction errors showing high decay (0.90)
                return FakeCursor(rows=[(0.95, -0.05) for _ in range(50)])
            if "UPDATE research_models SET status='DECAYING'" in q:
                self.model_status = 'DECAYING'
                return FakeCursor()
            if "UPDATE research_models SET status='RETIRED'" in q:
                self.model_status = 'RETIRED'
                return FakeCursor()
            if "INSERT INTO live_incidents" in q:
                self.live_incidents.append((q, p))
                return FakeCursor()
            if "SELECT id FROM research_jobs WHERE market=%s" in q:
                return FakeCursor(row=None)
            if "INSERT INTO research_jobs" in q:
                self.queued_jobs.append((q, p))
                return FakeCursor()
            if "SELECT status, market, symbol" in q:
                return FakeCursor(row=(self.model_status, 'spot', 'BTCUSDT', '1m'))
            return FakeCursor()

    conn = FakeConn()

    class FakePool:
        def connection(self):
            return conn

    class FakeDB:
        pool = FakePool()
        async def audit(self, actor, action, outcome, meta=None):
            conn.audits.append((actor, action, outcome, meta))

    class FakeRepo:
        db = FakeDB()
        async def mark_model_decaying(self, mid, reasons):
            from trader.intelligence.repository import ResearchRepo
            return await ResearchRepo(FakeDB()).mark_model_decaying(mid, reasons)
        async def retire_canary_drift(self, mid, reasons):
            from trader.intelligence.repository import ResearchRepo
            return await ResearchRepo(FakeDB()).retire_canary_drift(mid, reasons)
        async def trigger_retraining_job(self, market, symbol, interval):
            from trader.intelligence.repository import ResearchRepo
            return await ResearchRepo(FakeDB()).trigger_retraining_job(market, symbol, interval)
        async def request(self, actor, market, symbol, interval, bars, horizon, friction_bps):
            jid = str(uuid.uuid4())
            conn.queued_jobs.append((jid, market, symbol))
            return jid

    class FakePaper:
        async def bars(self, market, symbol, interval, count):
            # Synthesize sudden volatility shock bars
            bars = []
            now = int(time.time() * 1000)
            base = now - 100 * 60000
            for i in range(100):
                o_ms = base + i * 60000
                c_ms = o_ms + 59999
                # First 50 bars normal price around 100; last 50 bars extreme turbulence 500-1000
                if i < 50:
                    p = Decimal('100.0')
                    h = p + Decimal('0.5')
                    l = p - Decimal('0.5')
                    v = Decimal('10.0')
                else:
                    # Volatility shock
                    p = Decimal(f'{500 + (i % 5) * 100}')
                    h = p + Decimal('80.0')
                    l = p - Decimal('80.0')
                    v = Decimal('500.0')
                bars.append(Bar(market, symbol, interval, o_ms, c_ms, p, h, l, p, v))
            return bars

    import time
    drifted = await check_concept_drift(FakeRepo(), FakePaper())
    assert model_id in drifted
    # Model demoted and retired
    assert conn.model_status == 'RETIRED'
    # Incident logged in live_incidents
    assert any('CONCEPT_DRIFT_INTERVENTION' in str(inc[1]) for inc in conn.live_incidents)
    # Retraining job automatically queued
    assert len(conn.queued_jobs) > 0


@pytest.mark.asyncio
async def test_execution_interlocking_on_decaying_model():
    from decimal import Decimal
    from trader.live.automation import maybe_schedule_spot

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self._row = row
            self._rows = rows or []
        async def fetchone(self):
            return self._row
        async def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self, held=Decimal('0.005')):
            self.held = held
            self.intents = []
            now = int(time.time() * 1000)
            base = now - 30 * 60000
            self.bars = []
            for i in range(25):
                o = base + i * 60000
                e = o + 59999
                self.bars.append((o, e, Decimal('100'), Decimal('101'), Decimal('99'), Decimal('100'), Decimal('10')))

        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def transaction(self):
            return self

        async def execute(self, q, p=None):
            if 'FROM live_accounts' in q:
                return FakeCursor(row=('READY', True, True, {'account': {'balances': [{'asset': 'BTC', 'free': str(self.held)}]}}))
            if 'FROM live_automation' in q:
                return FakeCursor(row=('BTCUSDT', Decimal('0.001'), 2, 5, 0, 'sha'))
            if 'FROM historical_bars' in q:
                return FakeCursor(rows=self.bars)
            if 'FROM instruments' in q:
                return FakeCursor(row=('BTC', [{'filterType': 'LOT_SIZE', 'stepSize': '0.0001'}, {'filterType': 'MIN_NOTIONAL', 'minNotional': '5.0'}]))
            if "WHERE market='spot' AND symbol=%s AND status='DECAYING'" in q:
                # Decaying model exists!
                return FakeCursor(row=('decaying-model-1',))
            if 'INSERT INTO live_order_intents' in q:
                self.intents.append((q, p))
                return FakeCursor()
            return FakeCursor()

    class FakeRepo:
        def __init__(self, held):
            self.conn = FakeConn(held)
        @property
        def db(self):
            conn = self.conn
            class DB:
                class Pool:
                    def connection(self):
                        return conn
                pool = Pool()
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

    import time
    # 1. When holding position and model is DECAYING: immediately sell to revert to flat cash
    repo_held = FakeRepo(held=Decimal('0.005'))
    res_held = await maybe_schedule_spot(repo_held, FakeSettings())
    assert res_held == 'SELL'
    assert len(repo_held.conn.intents) == 1
    intent = repo_held.conn.intents[0][1][2].obj
    assert intent['side'] == 'SELL'
    assert intent['reason'] == 'DRIFT_REVERT_TO_CASH'

    # 2. When flat (held == 0) and model is DECAYING: pause entries
    repo_flat = FakeRepo(held=Decimal('0.0'))
    res_flat = await maybe_schedule_spot(repo_flat, FakeSettings())
    assert res_flat == 'paused_by_drift_decay'
    assert len(repo_flat.conn.intents) == 0


def test_20_level_depth_imbalance_and_weighted_skew():
    from trader.intelligence.features import (
        calculate_depth_imbalance,
        calculate_weighted_depth_skew,
        calculate_order_flow_imbalance,
        extract_microstructure_context,
        make_features,
        MICROSTRUCTURE_FEATURE_NAMES,
    )
    from trader.paper.history import Bar

    # Construct 20 synthetic depth levels: bids [price, qty], asks [price, qty]
    bids = [[f"{70000.0 - i * 5.0}", f"{1.0 + i * 0.1}"] for i in range(20)]
    asks = [[f"{70005.0 + i * 5.0}", f"{1.0 + i * 0.05}"] for i in range(20)]

    # 1. Depth imbalance across 20 levels
    imb = calculate_depth_imbalance(bids, asks, levels=20)
    assert isinstance(imb, float)
    assert -1.0 <= imb <= 1.0
    # Bids have more volume (1.0 + i*0.1 vs 1.0 + i*0.05) -> positive imbalance
    assert imb > 0.0

    # 2. Weighted depth skew (top-of-book levels receive higher decay weights)
    skew = calculate_weighted_depth_skew(bids, asks, levels=20)
    assert isinstance(skew, float)
    assert -1.0 <= skew <= 1.0
    assert skew > 0.0

    # Perfectly balanced test
    bal_bids = [[f"{100 - i}", "5.0"] for i in range(20)]
    bal_asks = [[f"{101 + i}", "5.0"] for i in range(20)]
    assert calculate_depth_imbalance(bal_bids, bal_asks) == 0.0
    assert calculate_weighted_depth_skew(bal_bids, bal_asks) == 0.0

    # Empty / degenerate inputs
    assert calculate_depth_imbalance([], []) == 0.0
    assert calculate_weighted_depth_skew([], []) == 0.0

    # 3. Order Flow Imbalance (OFI) across consecutive depth snapshots
    # Scenario: Bid price moves up from 70000 to 70001 (aggressive buying)
    prev_bids = [[f"{70000.0 - i * 5.0}", "2.0"] for i in range(20)]
    prev_asks = [[f"{70005.0 + i * 5.0}", "2.0"] for i in range(20)]
    curr_bids = [[f"{70001.0 - i * 5.0}", "3.0"] for i in range(20)]
    curr_asks = [[f"{70006.0 + i * 5.0}", "1.5"] for i in range(20)]

    ofi = calculate_order_flow_imbalance(prev_bids, prev_asks, curr_bids, curr_asks, levels=20)
    assert isinstance(ofi, float)
    assert -1.0 <= ofi <= 1.0
    assert ofi > 0.0  # Buying push produces positive OFI

    # 4. Context extraction from 20-level depth payload
    depth_payload = {
        'bids': bids,
        'asks': asks,
        'last_update_id': 12345678,
    }
    prev_depth_payload = {
        'bids': prev_bids,
        'asks': prev_asks,
        'last_update_id': 12345600,
    }
    ctx = extract_microstructure_context(
        depth_payload=depth_payload,
        prev_depth_payload=prev_depth_payload,
    )
    assert 'book_imbalance' in ctx
    assert 'depth_imbalance_20' in ctx
    assert 'weighted_depth_skew' in ctx
    assert 'order_flow_imbalance' in ctx
    assert ctx['depth_levels'] == 20
    assert ctx['book_imbalance'] == skew

    # 5. Fallback to book_ticker when depth_payload is absent
    bt_ctx = extract_microstructure_context(
        book_ticker_payload={'best_bid': '100', 'best_ask': '101', 'bid_qty': '8', 'ask_qty': '2'}
    )
    assert bt_ctx['book_imbalance'] == (8 - 2) / (8 + 2)

    # 6. Verify make_features incorporates 20-level depth context into feature row
    from decimal import Decimal
    bars = [
        Bar('spot', 'BTCUSDT', '1m', 1000 + i * 60000, 1000 + (i + 1) * 60000 - 1,
            Decimal('100.0'), Decimal('102.0'), Decimal('99.0'), Decimal('101.0'), Decimal('50.0'))
        for i in range(30)
    ]
    features = make_features(bars, context=ctx, feature_names=MICROSTRUCTURE_FEATURE_NAMES)
    assert len(features) == 8
    # Feature index 12 is book_imbalance_proxy in 15-feature schema
    last_row = features[-1]
    assert round(last_row.values[12], 4) == round(skew, 4)





