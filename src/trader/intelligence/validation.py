"""Combinatorial Purged Cross-Validation (CPCV) and Deflated Sharpe Ratio (DSR).

Implements Marcos López de Prado's methodologies (Advances in Financial Machine Learning,
Chapters 7 & 12; Bailey & López de Prado 2014) to eliminate lookahead bias, prevent
backtest overfitting, and statistically deflate Sharpe ratios for multiple testing.
"""
from __future__ import annotations

import math
from itertools import combinations
from statistics import mean, variance
from typing import Sequence

try:
    from scipy.stats import norm
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

EULER_MASCHERONI = 0.577215664901532860606512


class DSRResult(float):
    """Deflated Sharpe Ratio result behaving as a float (dsr_stat) and unpackable tuple (dsr_stat, p_value)."""
    dsr_stat: float
    p_value: float

    def __new__(cls, dsr_stat: float, p_value: float):
        instance = super().__new__(cls, dsr_stat)
        instance.dsr_stat = float(dsr_stat)
        instance.p_value = float(p_value)
        return instance

    def __iter__(self):
        yield self.dsr_stat
        yield self.p_value

    def __getitem__(self, index: int) -> float:
        return (self.dsr_stat, self.p_value)[index]

    def __repr__(self) -> str:
        return f"DSRResult(dsr_stat={self.dsr_stat:.4f}, p_value={self.p_value:.4f})"


def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    if _HAS_SCIPY:
        return float(norm.cdf(x))
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Standard normal quantile function (probit)."""
    if _HAS_SCIPY:
        return float(norm.ppf(p))
    # Acklam's inverse normal approximation
    if p <= 0.0 or p >= 1.0:
        raise ValueError("p must be in (0, 1)")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680133280037667e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00)
    p_low, p_high = 0.02425, 1.0 - 0.02425
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5])*q / \
               (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
            ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)


def cpcv_splits(n_samples: int, n_groups: int = 5, k_test_groups: int = 2,
                horizon: int = 1, embargo_pct: float = 0.01) -> list[tuple[list[int], list[int]]]:
    """Generates Combinatorial Purged Cross-Validation splits.

    - Partitions samples into n_groups contiguous chronological blocks.
    - Forms C(n_groups, k_test_groups) combinations of test groups.
    - Purges training samples whose label interval overlaps any test block.
    - Embargoes training samples immediately following any test block.
    """
    if n_samples < 50:
        raise ValueError('Insufficient samples for CPCV (minimum 50 required)')
    if not (1 <= k_test_groups < n_groups <= 10):
        raise ValueError('Invalid group parameters: must have 1 <= k < N <= 10')

    group_size = n_samples // n_groups
    groups: list[tuple[int, int]] = []
    for g in range(n_groups):
        start = g * group_size
        end = n_samples if g == n_groups - 1 else (g + 1) * group_size
        groups.append((start, end))

    embargo_len = max(horizon, int(n_samples * embargo_pct))
    splits: list[tuple[list[int], list[int]]] = []

    for test_comb in combinations(range(n_groups), k_test_groups):
        test_ranges = [groups[g] for g in test_comb]
        test_indices: list[int] = []
        for start, end in test_ranges:
            test_indices.extend(range(start, end))

        train_indices: list[int] = []
        for g in range(n_groups):
            if g in test_comb:
                continue
            g_start, g_end = groups[g]
            for i in range(g_start, g_end):
                # 1. Purging: forward horizon [i, i + horizon] must not overlap any test block [t_start, t_end)
                overlaps = any(not (i + horizon < t_start or i >= t_end) for t_start, t_end in test_ranges)
                if overlaps:
                    continue
                # 2. Embargoing: sample i must not fall into the post-test auto-correlation buffer
                in_embargo = any(0 <= i - t_end < embargo_len for _, t_end in test_ranges)
                if in_embargo:
                    continue
                train_indices.append(i)

        if train_indices and test_indices:
            splits.append((train_indices, test_indices))

    return splits


def calculate_moments(returns: Sequence[float]) -> tuple[float, float, float, float]:
    """Calculate mean, standard deviation, skewness, and kurtosis of returns."""
    n = len(returns)
    if n < 3:
        return 0.0, 0.0, 0.0, 3.0
    mu = mean(returns)
    var = sum((x - mu) ** 2 for x in returns) / (n - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0.0:
        return mu, 0.0, 0.0, 3.0
    skewness = (sum((x - mu) ** 3 for x in returns) / n) / (std ** 3)
    kurtosis = (sum((x - mu) ** 4 for x in returns) / n) / (std ** 4)
    return mu, std, skewness, kurtosis


def calculate_sharpe(returns: Sequence[float]) -> float:
    """Compute per-trade annualized Sharpe ratio proxy."""
    if len(returns) < 2:
        return 0.0
    mu = mean(returns)
    var = variance(returns)
    if var <= 0:
        return 0.0
    return mu / math.sqrt(var)


def probability_of_backtest_overfitting(
    in_sample_scores: list[list[float]] | list[tuple[list[float], list[float]]],
    out_of_sample_scores: list[list[float]] | None = None
) -> float:
    """Calculate Probability of Backtest Overfitting (PBO) across combinatorial folds.

    Supports either:
    1. Two matrices: (in_sample_scores, out_of_sample_scores), each of shape (n_splits, n_configurations).
    2. A single list of (is_scores, oos_scores) pairs, or a matrix of evaluation paths.

    PBO = Proportion of splits where the in-sample optimal strategy performs below
    the median out-of-sample (relative rank r > 0.5).
    """
    if out_of_sample_scores is None:
        if not in_sample_scores:
            return 0.0
        first = in_sample_scores[0]
        if isinstance(first, (tuple, list)) and len(first) == 2 and isinstance(first[0], (tuple, list)):
            is_mat = [list(row[0]) for row in in_sample_scores]  # type: ignore
            oos_mat = [list(row[1]) for row in in_sample_scores]  # type: ignore
            return probability_of_backtest_overfitting(is_mat, oos_mat)
        elif isinstance(first, (tuple, list)) and len(first) >= 4:
            half = len(first) // 2
            is_mat = [[float(row[j]) for j in range(half)] for row in in_sample_scores]  # type: ignore
            oos_mat = [[float(row[j]) for j in range(half, len(first))] for row in in_sample_scores]  # type: ignore
            return probability_of_backtest_overfitting(is_mat, oos_mat)
        else:
            return 0.0

    n_splits = len(in_sample_scores)
    if n_splits == 0 or len(out_of_sample_scores) != n_splits:
        return 0.0

    overfit_count = 0
    valid_splits = 0
    for is_row, oos_row in zip(in_sample_scores, out_of_sample_scores):  # type: ignore
        m = len(is_row)
        if m < 2 or len(oos_row) != m:
            continue
        valid_splits += 1
        # Find index of best in-sample strategy
        best_is_idx = max(range(m), key=lambda idx: is_row[idx])
        best_oos_score = oos_row[best_is_idx]
        # Rank of best_is_idx in out-of-sample (1 = best, m = worst)
        sorted_oos = sorted(oos_row, reverse=True)
        rank = sorted_oos.index(best_oos_score) + 1
        relative_rank = rank / (m + 1)
        if relative_rank > 0.5:
            overfit_count += 1

    return overfit_count / valid_splits if valid_splits > 0 else 0.0


def deflated_sharpe_ratio(
    estimated_sharpe: float,
    trials: int = 1,
    variance_sharpe: float = 0.0,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
    sample_length: int = 100
) -> DSRResult:
    """Calculate the Deflated Sharpe Ratio (DSR) and its asymptotic p-value.

    estimated_sharpe (SR*): Sharpe ratio of the selected strategy.
    trials (K): Total number of configurations / trials tested.
    variance_sharpe: Variance of Sharpe ratios across all tested trials.
    sample_length (T): Number of observation periods / trades.

    Returns:
        DSRResult behaving as a float (dsr_stat) and unpackable tuple (dsr_stat, p_value).
    """
    if trials <= 1 or variance_sharpe <= 0.0:
        expected_max_sr = 0.0
    else:
        var_sr = max(1e-12, variance_sharpe)
        p1 = max(1e-6, min(1.0 - 1e-6, 1.0 - 1.0 / trials))
        p2 = max(1e-6, min(1.0 - 1e-6, 1.0 - 1.0 / (trials * math.e)))
        z1 = _norm_ppf(p1)
        z2 = _norm_ppf(p2)
        expected_max_sr = math.sqrt(var_sr) * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)

    # Standard deviation of Sharpe ratio adjusted for skewness and kurtosis
    t_len = max(2, sample_length)
    denom_term = 1.0 - skewness * estimated_sharpe + ((kurtosis - 1.0) / 4.0) * (estimated_sharpe ** 2)
    denom = math.sqrt(max(1e-6, denom_term) / (t_len - 1))

    dsr_stat = (estimated_sharpe - expected_max_sr) / denom if denom > 0 else 0.0
    p_value = 1.0 - _norm_cdf(dsr_stat)

    return DSRResult(dsr_stat, max(0.0, min(1.0, p_value)))


def validate_cpcv(samples, horizon: int = 1, friction_bps: float = 15.0,
                  n_groups: int = 5, k_test_groups: int = 2,
                  random_state: int = 23, feature_names: tuple[str, ...] | None = None) -> dict:
    """Run full CPCV evaluation over training samples and calculate PBO and DSR."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from trader.intelligence.model import LinearProbabilityModel
    from trader.intelligence.training import binary_metrics, evaluate

    n_samples = len(samples)
    splits = cpcv_splits(n_samples, n_groups, k_test_groups, horizon=horizon)
    choices = (0.50, 0.55, 0.60, 0.65, 0.70)

    in_sample_scores: list[list[float]] = []
    out_of_sample_scores: list[list[float]] = []
    all_oos_sharpes: list[float] = []
    all_oos_briers: list[float] = []
    all_returns: list[float] = []
    path_net_returns: list[float] = []

    for train_idx, test_idx in splits:
        train_samples = [samples[i] for i in train_idx]
        test_samples = [samples[i] for i in test_idx]

        y_train = [y for _, y, _ in train_samples]
        if len(set(y_train)) < 2:
            continue

        x_train = [r.values for r, _, _ in train_samples]
        x_test = [r.values for r, _, _ in test_samples]
        y_test = [y for _, y, _ in test_samples]

        scaler = StandardScaler().fit(x_train)
        clf = LogisticRegression(max_iter=300, random_state=random_state, class_weight='balanced')
        clf.fit(scaler.transform(x_train), y_train)

        model = LinearProbabilityModel(
            tuple(map(float, clf.coef_[0])), float(clf.intercept_[0]),
            tuple(map(float, scaler.mean_)), tuple(map(float, scaler.scale_)), 0.55,
            feature_names=feature_names
        )

        p_train = [model.probability(r) for r, _, _ in train_samples]
        p_test = [model.probability(r) for r, _, _ in test_samples]

        is_row = []
        oos_row = []
        for th in choices:
            is_eval = evaluate(train_samples, p_train, th, horizon)
            oos_eval = evaluate(test_samples, p_test, th, horizon)
            is_row.append(is_eval['net_return_fraction'])
            oos_row.append(oos_eval['net_return_fraction'])

        in_sample_scores.append(is_row)
        out_of_sample_scores.append(oos_row)

        # Select best threshold on in-sample and measure OOS Sharpe
        best_th = choices[max(range(len(choices)), key=lambda i: is_row[i])]
        oos_best = evaluate(test_samples, p_test, best_th, horizon)
        path_net_returns.append(oos_best['net_return_fraction'])

        oos_trades = [ret for _, ret, _ in [
            (row.index, ret, p) for (row, _, ret), p in zip(test_samples, p_test) if p >= best_th
        ]]
        all_returns.extend(oos_trades)
        sr = calculate_sharpe(oos_trades)
        all_oos_sharpes.append(sr)
        all_oos_briers.append(binary_metrics(y_test, p_test)['brier'])

    pbo = probability_of_backtest_overfitting(in_sample_scores, out_of_sample_scores)
    mean_sr = mean(all_oos_sharpes) if all_oos_sharpes else 0.0
    var_sr = variance(all_oos_sharpes) if len(all_oos_sharpes) > 1 else 0.0
    _, _, skew, kurt = calculate_moments(all_returns)

    total_trials = len(splits) * len(choices)
    dsr_result = deflated_sharpe_ratio(
        mean_sr, trials=total_trials, variance_sharpe=var_sr,
        skewness=skew, kurtosis=kurt, sample_length=len(all_returns)
    )
    dsr_stat, dsr_pval = dsr_result.dsr_stat, dsr_result.p_value

    min_net_ret = min(path_net_returns) if path_net_returns else 0.0
    mean_net_ret = mean(path_net_returns) if path_net_returns else 0.0

    return {
        'pbo': round(pbo, 4),
        'dsr_stat': round(dsr_stat, 4),
        'dsr_p_value': round(dsr_pval, 4),
        'n_splits': len(splits),
        'mean_test_sharpe': round(mean_sr, 4),
        'mean_test_brier': round(mean(all_oos_briers), 4) if all_oos_briers else 0.25,
        'path_net_returns': [round(r, 6) for r in path_net_returns],
        'min_test_net_return': round(min_net_ret, 6),
        'mean_test_net_return': round(mean_net_ret, 6),
        'cpcv_pass': bool(pbo < 0.15 and dsr_pval < 0.05 and min_net_ret > 0),
    }
