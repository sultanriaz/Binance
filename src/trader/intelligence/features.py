"""Chronological, closed-candle feature generation without labels or future observations.

A row at t depends only on bars through t. Target and execution calculations
live in training.py so inference cannot accidentally receive future information.
Phase 1 introduces 6 microstructure and derivatives alpha indicators alongside core momentum.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, log, sqrt
from typing import Sequence

from trader.paper.history import Bar, validate

LEGACY_FEATURE_NAMES = (
    'return_1', 'return_3', 'return_8', 'return_20', 'range_1',
    'body_1', 'volume_ratio_10', 'volatility_20', 'rsi_14'
)

MICROSTRUCTURE_FEATURE_NAMES = (
    # Core price-action momentum & volatility
    'return_1', 'return_3', 'return_8', 'return_20', 'range_1',
    'body_1', 'volume_ratio_10', 'volatility_20', 'rsi_14',
    # Phase 1: Microstructure & Derivatives Alpha additions
    'wick_imbalance',       # (lower_wick - upper_wick) / range: aggressive absorption vs overhead supply
    'atr_expansion_20',     # current_tr / atr_20 - 1.0: volatility expansion / compression squeeze
    'volume_momentum_5',    # 5-bar volume-weighted price change momentum
    'book_imbalance_proxy', # Order flow imbalance / close location value proxy in [-1.0, 1.0]
    'basis_ema_20',         # (close - EMA_20) / EMA_20: dynamic mean-reversion stretch
    'funding_drift_proxy',  # Carry / basis drift: (close - VWAP_20) / close or live funding bps
)

FEATURE_NAMES = LEGACY_FEATURE_NAMES
WARMUP = 22



@dataclass(frozen=True)
class FeatureRow:
    index: int
    open_ms: int
    values: tuple[float, ...]


def calculate_depth_imbalance(bids: Sequence[Any], asks: Sequence[Any], levels: int = 20) -> float:
    """Calculate orderbook depth imbalance across up to N price levels: (Q_b - Q_a) / (Q_b + Q_a)."""
    try:
        sum_b = sum(float(b[1]) for b in bids[:levels] if len(b) >= 2)
        sum_a = sum(float(a[1]) for a in asks[:levels] if len(a) >= 2)
        total = sum_b + sum_a
        if total <= 0:
            return 0.0
        return max(-1.0, min(1.0, (sum_b - sum_a) / total))
    except (ValueError, TypeError, IndexError):
        return 0.0


def calculate_weighted_depth_skew(bids: Sequence[Any], asks: Sequence[Any], levels: int = 20) -> float:
    """Calculate decay-weighted depth skew across up to N price levels.

    Levels closest to top-of-book receive the highest weighting (1/sqrt(i+1)), capturing immediate
    buying/selling pressure while incorporating deep book liquidity.
    """
    try:
        wb = 0.0
        wa = 0.0
        for i, b in enumerate(bids[:levels]):
            if len(b) >= 2:
                weight = 1.0 / sqrt(i + 1.0)
                wb += weight * float(b[1])
        for i, a in enumerate(asks[:levels]):
            if len(a) >= 2:
                weight = 1.0 / sqrt(i + 1.0)
                wa += weight * float(a[1])
        total = wb + wa
        if total <= 0:
            return 0.0
        return max(-1.0, min(1.0, (wb - wa) / total))
    except (ValueError, TypeError, IndexError):
        return 0.0


def calculate_order_flow_imbalance(prev_bids: Sequence[Any] | None,
                                   prev_asks: Sequence[Any] | None,
                                   curr_bids: Sequence[Any],
                                   curr_asks: Sequence[Any],
                                   levels: int = 20) -> float:
    """Multi-level Order Flow Imbalance (OFI) based on Cont, Kukanov & Stoikov (2014).

    Computes net order flow innovations across consecutive depth snapshots:
    d_B = q_b(t) if p_b(t) > p_b(t-1); (q_b(t) - q_b(t-1)) if p_b(t) == p_b(t-1); -q_b(t-1) if p_b(t) < p_b(t-1)
    d_A = q_a(t) if p_a(t) < p_a(t-1); (q_a(t) - q_a(t-1)) if p_a(t) == p_a(t-1); -q_a(t-1) if p_a(t) > p_a(t-1)
    OFI = sum(w_k * (d_B - d_A)) / normalized_denominator
    """
    if not prev_bids or not prev_asks or not curr_bids or not curr_asks:
        return 0.0
    try:
        ofi_sum = 0.0
        denom = 0.0
        max_levels = min(levels, len(prev_bids), len(curr_bids), len(prev_asks), len(curr_asks))
        for i in range(max_levels):
            w = 1.0 / sqrt(i + 1.0)
            p_b_prev, q_b_prev = float(prev_bids[i][0]), float(prev_bids[i][1])
            p_b_curr, q_b_curr = float(curr_bids[i][0]), float(curr_bids[i][1])
            if p_b_curr > p_b_prev:
                delta_b = q_b_curr
            elif p_b_curr == p_b_prev:
                delta_b = q_b_curr - q_b_prev
            else:
                delta_b = -q_b_prev

            p_a_prev, q_a_prev = float(prev_asks[i][0]), float(prev_asks[i][1])
            p_a_curr, q_a_curr = float(curr_asks[i][0]), float(curr_asks[i][1])
            if p_a_curr < p_a_prev:
                delta_a = q_a_curr
            elif p_a_curr == p_a_prev:
                delta_a = q_a_curr - q_a_prev
            else:
                delta_a = -q_a_prev

            ofi_sum += w * (delta_b - delta_a)
            denom += w * (max(q_b_curr, q_b_prev) + max(q_a_curr, q_a_prev))

        if denom <= 0:
            return 0.0
        return max(-1.0, min(1.0, ofi_sum / denom))
    except (ValueError, TypeError, IndexError):
        return 0.0


def extract_microstructure_context(book_ticker_payload: dict | None = None,
                                   mark_price_payload: dict | None = None,
                                   depth_payload: dict | None = None,
                                   prev_depth_payload: dict | None = None) -> dict:
    """Extract normalized orderbook imbalance and funding drift from real-time events.

    If 20-level depth_payload is provided, calculates high-precision multi-level
    depth imbalance, weighted depth skew, and Order Flow Imbalance (OFI).
    Otherwise, gracefully falls back to top-of-book ticker.
    """
    context = {}
    if depth_payload and isinstance(depth_payload.get('bids'), list) and isinstance(depth_payload.get('asks'), list):
        bids = depth_payload['bids']
        asks = depth_payload['asks']
        if bids and asks:
            imbalance = calculate_depth_imbalance(bids, asks, levels=20)
            weighted_skew = calculate_weighted_depth_skew(bids, asks, levels=20)
            context['depth_imbalance_20'] = imbalance
            context['weighted_depth_skew'] = weighted_skew
            context['depth_levels'] = min(len(bids), len(asks), 20)
            context['book_imbalance'] = weighted_skew

            if prev_depth_payload and isinstance(prev_depth_payload.get('bids'), list) and isinstance(prev_depth_payload.get('asks'), list):
                context['order_flow_imbalance'] = calculate_order_flow_imbalance(
                    prev_depth_payload['bids'], prev_depth_payload['asks'],
                    bids, asks, levels=20
                )
    elif book_ticker_payload:
        try:
            bid_qty = float(book_ticker_payload.get('bid_qty', 0))
            ask_qty = float(book_ticker_payload.get('ask_qty', 0))
            total = bid_qty + ask_qty
            if total > 0:
                context['book_imbalance'] = (bid_qty - ask_qty) / total
        except (ValueError, TypeError):
            pass
    if mark_price_payload:
        try:
            fr_str = mark_price_payload.get('funding_rate', '')
            if fr_str:
                context['funding_rate'] = float(fr_str)
        except (ValueError, TypeError):
            pass
    return context


def make_features(bars: Sequence[Bar], context: dict | None = None,
                  feature_names: tuple[str, ...] | None = None) -> list[FeatureRow]:
    if len(bars) < WARMUP + 1:
        raise ValueError('At least 23 closed candles required for features')
    validate(list(bars))
    close = [float(b.c) for b in bars]
    high = [float(b.h) for b in bars]
    low = [float(b.l) for b in bars]
    open_p = [float(b.o) for b in bars]
    vols = [float(b.volume) for b in bars]

    target_schema = feature_names if feature_names is not None else FEATURE_NAMES
    use_legacy = (target_schema == LEGACY_FEATURE_NAMES or len(target_schema) == 9)


    out = []
    for i in range(WARMUP, len(bars)):
        b = bars[i]
        recent = [log(close[j] / close[j - 1]) for j in range(i - 19, i + 1)]
        mean_ret = sum(recent) / 20.0
        gains = sum(max(close[j] - close[j - 1], 0.0) for j in range(i - 13, i + 1))
        losses = sum(max(close[j - 1] - close[j], 0.0) for j in range(i - 13, i + 1))
        rsi = gains / (gains + losses) if gains + losses else 0.5
        vols_10 = vols[i - 9:i + 1]
        avg_volume = sum(vols_10) / len(vols_10)

        # 9 Core features
        f_ret1 = close[i] / close[i - 1] - 1.0
        f_ret3 = close[i] / close[i - 3] - 1.0
        f_ret8 = close[i] / close[i - 8] - 1.0
        f_ret20 = close[i] / close[i - 20] - 1.0
        f_range1 = float((b.h - b.l) / b.c)
        f_body1 = float((b.c - b.o) / b.o)
        f_vol_ratio = float(b.volume) / avg_volume - 1.0 if avg_volume else 0.0
        f_volat20 = sqrt(sum((x - mean_ret)**2 for x in recent) / 20.0)
        f_rsi = rsi

        core_values = (f_ret1, f_ret3, f_ret8, f_ret20, f_range1,
                       f_body1, f_vol_ratio, f_volat20, f_rsi)

        if use_legacy:
            if not all(isfinite(v) for v in core_values):
                raise ValueError('Non-finite feature encountered')
            out.append(FeatureRow(i, b.open_ms, core_values))
            continue

        # Microstructure feature 1: Wick Imbalance (absorption vs overhead supply)
        candle_range = high[i] - low[i]
        if candle_range > 0:
            upper_wick = high[i] - max(open_p[i], close[i])
            lower_wick = min(open_p[i], close[i]) - low[i]
            f_wick_imbalance = (lower_wick - upper_wick) / candle_range
        else:
            f_wick_imbalance = 0.0

        # Microstructure feature 2: ATR Expansion 20 (volatility breakout / squeeze)
        tr_window = [max(high[j] - low[j],
                         abs(high[j] - close[j - 1]),
                         abs(low[j] - close[j - 1]))
                     for j in range(i - 19, i + 1)]
        atr_20 = sum(tr_window) / 20.0
        current_tr = tr_window[-1]
        f_atr_exp = (current_tr / atr_20 - 1.0) if atr_20 > 0 else 0.0

        # Microstructure feature 3: Volume Momentum 5 (volume-weighted price acceleration)
        v5 = vols[i - 4:i + 1]
        sum_v5 = sum(v5)
        if sum_v5 > 0:
            f_vol_mom5 = sum(v * (close[j] / close[j - 1] - 1.0) for v, j in zip(v5, range(i - 4, i + 1))) / sum_v5
        else:
            f_vol_mom5 = 0.0

        # Microstructure feature 4: Book Imbalance / Close Location Value Proxy
        if context and context.get('book_imbalance') is not None and i == len(bars) - 1:
            f_book_imbalance = max(-1.0, min(1.0, float(context['book_imbalance'])))
        else:
            if candle_range > 0:
                f_book_imbalance = float((close[i] - low[i]) - (high[i] - close[i])) / candle_range
            else:
                f_book_imbalance = 0.0

        # Microstructure feature 5: Basis EMA 20 (dynamic equilibrium stretch)
        alpha = 2.0 / (20.0 + 1.0)
        ema = close[i - 19]
        for j in range(i - 18, i + 1):
            ema = alpha * close[j] + (1.0 - alpha) * ema
        f_basis_ema = (close[i] - ema) / ema if ema > 0 else 0.0

        # Microstructure feature 6: Funding Drift / Basis Drift Proxy
        if context and context.get('funding_rate') is not None and i == len(bars) - 1:
            f_funding_drift = float(context['funding_rate']) * 10000.0  # Basis points
        else:
            v20 = vols[i - 19:i + 1]
            sum_v20 = sum(v20)
            if sum_v20 > 0:
                vwap_20 = sum(v * c for v, c in zip(v20, close[i - 19:i + 1])) / sum_v20
                f_funding_drift = (close[i] - vwap_20) / close[i] if close[i] > 0 else 0.0
            else:
                f_funding_drift = 0.0

        all_values = (*core_values, f_wick_imbalance, f_atr_exp, f_vol_mom5,
                      f_book_imbalance, f_basis_ema, f_funding_drift)

        if not all(isfinite(v) for v in all_values):
            raise ValueError('Non-finite feature encountered')
        out.append(FeatureRow(i, b.open_ms, all_values))

    return out


def regime(row: FeatureRow) -> str:
    volatility = row.values[7]  # volatility_20
    momentum = row.values[3]    # return_20
    if len(row.values) >= 15:
        atr_exp = row.values[10]
        if volatility > 0.02 or atr_exp > 1.2:
            return 'HIGH_VOLATILITY'
    if volatility > 0.02:
        return 'HIGH_VOLATILITY'
    if abs(momentum) > 0.03:
        return 'TRENDING_UP' if momentum > 0 else 'TRENDING_DOWN'
    return 'LOW_MOMENTUM'


def is_closed(bars: Sequence[Bar], now_ms: int) -> bool:
    return all(int(b.close_ms) < now_ms for b in bars)
