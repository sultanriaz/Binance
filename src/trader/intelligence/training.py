"""Point-in-time lab training with purged chronological splits and held-out evaluation.

Proposals are explicitly non-executable research. Uses optional scikit-learn
only in the isolated research worker, not the exchange execution container.
"""
from __future__ import annotations

import hashlib
import math
from statistics import mean

from trader.intelligence.features import FEATURE_NAMES, make_features
from trader.intelligence.model import LinearProbabilityModel
from trader.paper.history import Bar, validate


def training_samples(bars: list[Bar], horizon: int = 1, round_trip_bps: float = 15.0,
                     feature_names: tuple[str, ...] | None = None):
    if horizon not in (1, 2, 3, 5):
        raise ValueError('Supported horizons: 1, 2, 3, 5 closed bars')
    if not 0 <= round_trip_bps <= 100:
        raise ValueError('Invalid friction assumption')
    validate(bars)
    rows = make_features(bars, feature_names=feature_names)
    out = []
    for row in rows:
        if row.index + horizon >= len(bars):
            continue
        # Decision at close t; entry is next candle open, exit at t+h close.
        buy = float(bars[row.index + 1].o)
        sale = float(bars[row.index + horizon].c)
        gross = sale / buy - 1
        net = gross - round_trip_bps / 10000
        out.append((row, int(net > 0), net))
    return out



def binary_metrics(labels, probs):
    if not labels:
        raise ValueError('Empty evaluation set')
    eps = 1e-12
    return {'brier': sum((p - y)**2 for y, p in zip(labels, probs))/len(labels),
            'log_loss': -sum(y * math.log(max(eps, min(1-eps, p))) +
                             (1-y)*math.log(max(eps, min(1-eps, 1-p)))
                             for y, p in zip(labels, probs))/len(labels),
            'positive_rate': sum(labels)/len(labels), 'n': len(labels)}


def evaluate(samples, probs, threshold: float, horizon: int):
    # Non-overlapping trades, no simultaneous capital re-use for overlap.
    chosen = [(row.index, ret, p) for (row, _, ret), p in zip(samples, probs)
              if p >= threshold]
    last_exit = -1
    returns = []
    for index, ret, _ in chosen:
        if index <= last_exit:
            continue
        returns.append(ret)
        last_exit = index + horizon
    compounded = 1.0
    peak = 1.0
    drawdown = 0.0
    for ret in returns:
        compounded *= max(0.0, 1.0 + ret)
        peak = max(peak, compounded)
        drawdown = max(drawdown, (peak-compounded)/peak)
    return {'trades': len(returns), 'net_return_fraction': compounded-1,
            'max_drawdown_fraction': drawdown,
            'mean_net_trade_return': mean(returns) if returns else None,
            'positive_trades': sum(x > 0 for x in returns)}


def train_candidate(bars: list[Bar], *, horizon: int = 1, friction_bps: float = 15,
                    random_state: int = 23, feature_names: tuple[str, ...] | None = None) -> dict:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    features_to_use = feature_names if feature_names is not None else FEATURE_NAMES
    if len(bars) < 350 or len(bars) > 20000:
        raise ValueError('Dataset requires 350..20,000 continuous closed bars')
    samples = training_samples(bars, horizon, friction_bps, feature_names=features_to_use)
    n = len(samples)
    cut_train = int(n * 0.6)
    cut_valid = int(n * 0.8)
    # Each target uses future candles; embargo removes labels overlapping next section.
    train = samples[:cut_train - horizon]
    valid = samples[cut_train:cut_valid - horizon]
    test = samples[cut_valid:]
    if min(len(train), len(valid), len(test)) < 50 or len({y for _,y,_ in train}) != 2:
        raise ValueError('Insufficient split size or train labels have only one class')
    x_train = [r.values for r,_,_ in train]
    y_train = [y for _,y,_ in train]
    scaler = StandardScaler().fit(x_train)  # Only train statistics.
    clf = LogisticRegression(max_iter=500, random_state=random_state,
                             class_weight='balanced').fit(scaler.transform(x_train), y_train)
    base = LinearProbabilityModel(tuple(map(float, clf.coef_[0])), float(clf.intercept_[0]),
                                  tuple(map(float,scaler.mean_)), tuple(map(float,scaler.scale_)), 0.55,
                                  feature_names=features_to_use)
    p_val = [base.probability(r) for r,_,_ in valid]
    # Threshold optimized on validation only, and NEVER on held-out test.
    choices = (0.50, 0.55, 0.60, 0.65, 0.70)
    scored = [(evaluate(valid, p_val, x, horizon), x) for x in choices]
    eligible = [pair for pair in scored if pair[0]['trades'] >= 5]
    threshold = max(eligible, key=lambda pair:pair[0]['net_return_fraction'])[1] if eligible else 0.70
    model = LinearProbabilityModel(base.weights, base.intercept, base.means, base.scales, threshold,
                                   feature_names=features_to_use)

    p_test = [model.probability(r) for r,_,_ in test]
    metrics = binary_metrics([y for _,y,_ in test], p_test)
    base_rate = sum(y for _,y,_ in train)/len(train)
    baseline = binary_metrics([y for _,y,_ in test], [base_rate]*len(test))
    trades = evaluate(test, p_test, threshold, horizon)

    cpcv_metrics = None
    if len(samples) >= 100:
        try:
            from trader.intelligence.validation import validate_cpcv
            cpcv_metrics = validate_cpcv(samples, horizon=horizon, friction_bps=friction_bps,
                                         feature_names=features_to_use, random_state=random_state)
        except Exception:
            cpcv_metrics = None

    metrics_payload = {
        **metrics, 'baseline_brier':baseline['brier'], 'baseline_log_loss':baseline['log_loss'],
        'holdout':trades, 'validation_threshold':threshold,
        'train_rows':len(train), 'validation_rows':len(valid), 'test_rows':len(test)
    }
    if cpcv_metrics is not None:
        metrics_payload['cpcv'] = cpcv_metrics

    dataset_hash = hashlib.sha256('\n'.join(f'{b.open_ms}:{b.o}:{b.h}:{b.l}:{b.c}:{b.volume}'
                                              for b in bars).encode()).hexdigest()
    return {'artifact':model.to_artifact(), 'metrics': metrics_payload,
        'data_sha256':dataset_hash, 'market':bars[0].market,'symbol':bars[0].symbol,
        'interval':bars[0].interval, 'horizon':horizon,
        'friction_bps':friction_bps,'first_open_ms':bars[0].open_ms,
        'last_open_ms':bars[-1].open_ms,
        'limitations':['Closed bars only; estimated friction, not real order fills',
            'Selection threshold derived from validation, never holdout',
            'Combinatorial Purged Cross-Validation (CPCV) with Deflated Sharpe Ratio applied',
            'No model promotion or market order is triggered by training']}


def candidate_quality(result: dict, minimum_trades: int = 10) -> tuple[bool, list[str]]:
    m = result['metrics']
    reasons = []
    if m['brier'] >= m['baseline_brier']:
        reasons.append('Brier score fails baseline')
    if m['log_loss'] >= m['baseline_log_loss']:
        reasons.append('Log loss fails baseline')
    if m['holdout']['trades'] < minimum_trades:
        reasons.append('Insufficient holdout trading sample')
    if m['holdout']['net_return_fraction'] <= 0:
        reasons.append('Nonpositive estimated after-friction holdout return')
    if m['holdout']['max_drawdown_fraction'] > .10:
        reasons.append('Holdout drawdown exceeds laboratory cap')
    if 'cpcv' in m and m['cpcv']:
        cpcv = m['cpcv']
        if cpcv.get('pbo', 0.0) >= 0.15:
            reasons.append(f"Probability of backtest overfitting (PBO) {cpcv['pbo']:.3f} >= 0.15")
        if cpcv.get('dsr_p_value', 0.0) >= 0.05:
            reasons.append(f"Deflated Sharpe Ratio p-value {cpcv['dsr_p_value']:.4f} >= 0.05")
        if cpcv.get('min_test_net_return', 0.0) <= 0:
            reasons.append(f"Nonpositive net return across combinatorial test paths (min: {cpcv.get('min_test_net_return', 0.0):.4f})")
    return not reasons, reasons

