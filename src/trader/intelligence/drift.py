"""Statistical Concept Drift Detection and Change-Point Analysis.

Implements two-sample Kolmogorov-Smirnov (KS) tests and Page-Hinkley sequential change-point
detection to detect non-stationarity, regime shifts, and prediction decay in production models.
"""
from __future__ import annotations

import math
from typing import Sequence

try:
    from scipy.stats import ks_2samp as _scipy_ks_2samp
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


def _kolmogorov_p_value(d_stat: float, n1: int, n2: int) -> float:
    """Asymptotic Kolmogorov distribution p-value approximation for two samples."""
    if d_stat <= 0.0 or n1 <= 0 or n2 <= 0:
        return 1.0
    en = math.sqrt((n1 * n2) / (n1 + n2))
    lam = (en + 0.12 + 0.11 / en) * d_stat
    if lam <= 0.0:
        return 1.0
    p = 0.0
    for k in range(1, 101):
        term = 2.0 * ((-1) ** (k - 1)) * math.exp(-2.0 * (k ** 2) * (lam ** 2))
        p += term
        if abs(term) < 1e-12:
            break
    return max(0.0, min(1.0, p))


def two_sample_ks_test(reference_sample: Sequence[float], current_sample: Sequence[float]) -> float:
    """Two-sample Kolmogorov-Smirnov test comparing empirical distributions.

    Null hypothesis H0: Both samples are drawn from the same underlying distribution.
    A p-value < 0.01 rejects H0 at 99% confidence, indicating statistical concept drift.

    Returns:
        float: Asymptotic p-value in [0.0, 1.0].
    """
    n1 = len(reference_sample)
    n2 = len(current_sample)
    if n1 < 5 or n2 < 5:
        return 1.0

    if _HAS_SCIPY:
        try:
            res = _scipy_ks_2samp(reference_sample, current_sample)
            return float(res.pvalue)
        except Exception:
            pass

    sorted1 = sorted(reference_sample)
    sorted2 = sorted(current_sample)
    all_values = sorted(set(sorted1 + sorted2))

    i1 = 0
    i2 = 0
    max_d = 0.0
    for v in all_values:
        while i1 < n1 and sorted1[i1] <= v:
            i1 += 1
        while i2 < n2 and sorted2[i2] <= v:
            i2 += 1
        d = abs(i1 / n1 - i2 / n2)
        if d > max_d:
            max_d = d

    return _kolmogorov_p_value(max_d, n1, n2)


def page_hinkley_drift(errors: Sequence[float], threshold: float = 5.0, delta: float = 0.005) -> bool:
    """Page-Hinkley sequential change-point detector for cumulative prediction error drift.

    Monitors whether cumulative prediction error deviates significantly from its historical mean.

    Args:
        errors: Sequence of observation errors (e.g. |p_up - y| or squared error).
        threshold: Cumulative deviation trigger threshold (default 5.0).
        delta: Magnitude parameter to absorb slight noise (default 0.005).

    Returns:
        bool: True if change-point drift is detected, False otherwise.
    """
    if not errors or len(errors) < 5:
        return False

    mean_val = 0.0
    cum_sum = 0.0
    min_cum_sum = 0.0

    for t, e in enumerate(errors, start=1):
        mean_val += (e - mean_val) / t
        cum_sum += (e - mean_val - delta)
        if cum_sum < min_cum_sum:
            min_cum_sum = cum_sum
        if (cum_sum - min_cum_sum) > threshold:
            return True

    return False


def detect_feature_drift(
    reference_matrix: Sequence[Sequence[float]],
    current_matrix: Sequence[Sequence[float]],
    feature_names: Sequence[str],
    alpha: float = 0.01
) -> tuple[int, list[str], dict[str, float]]:
    """Runs KS drift test on all features between reference and current distribution.

    Returns:
        (drifting_count, drifting_feature_names, p_values_by_feature)
    """
    if not reference_matrix or not current_matrix or not feature_names:
        return 0, [], {}

    p_values: dict[str, float] = {}
    drifting: list[str] = []

    num_features = min(len(feature_names), len(reference_matrix[0]), len(current_matrix[0]))
    for idx in range(num_features):
        fname = feature_names[idx]
        ref_col = [row[idx] for row in reference_matrix]
        cur_col = [row[idx] for row in current_matrix]
        pval = two_sample_ks_test(ref_col, cur_col)
        p_values[fname] = round(pval, 6)
        if pval < alpha:
            drifting.append(fname)

    return len(drifting), drifting, p_values


def evaluate_model_drift(
    reference_matrix: Sequence[Sequence[float]],
    current_matrix: Sequence[Sequence[float]],
    errors: Sequence[float],
    feature_names: Sequence[str],
    ks_threshold_features: int = 2,
    ks_alpha: float = 0.01,
    ph_threshold: float = 5.0
) -> tuple[bool, list[str]]:
    """Evaluates combined KS feature distribution drift and Page-Hinkley error drift.

    Returns:
        (drift_detected: bool, reasons: list[str])
    """
    reasons: list[str] = []

    # 1. KS feature drift
    drift_count, drifting_names, _ = detect_feature_drift(
        reference_matrix, current_matrix, feature_names, alpha=ks_alpha
    )
    if drift_count > ks_threshold_features:
        reasons.append(
            f"Kolmogorov-Smirnov feature drift detected on {drift_count} features (threshold > {ks_threshold_features}): {', '.join(drifting_names)}"
        )

    # 2. Page-Hinkley change-point drift
    ph_triggered = page_hinkley_drift(errors, threshold=ph_threshold)
    if ph_triggered:
        reasons.append(
            f"Page-Hinkley change-point detector triggered (cumulative prediction error exceeded threshold {ph_threshold})"
        )

    return bool(reasons), reasons

