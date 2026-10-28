"""Chronological, closed-candle feature generation without labels or future observations.

A row at t depends only on bars through t. Target and execution calculations
live in training.py so inference cannot accidentally receive future information.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, log, sqrt
from typing import Sequence

from trader.paper.history import Bar, validate

FEATURE_NAMES = ('return_1', 'return_3', 'return_8', 'return_20', 'range_1',
                 'body_1', 'volume_ratio_10', 'volatility_20', 'rsi_14')
WARMUP = 22


@dataclass(frozen=True)
class FeatureRow:
    index: int
    open_ms: int
    values: tuple[float, ...]


def make_features(bars: Sequence[Bar]) -> list[FeatureRow]:
    if len(bars) < WARMUP + 1:
        raise ValueError('At least 23 closed candles required for features')
    validate(list(bars))
    close = [float(b.c) for b in bars]
    out = []
    for i in range(WARMUP, len(bars)):
        b = bars[i]
        recent = [log(close[j] / close[j - 1]) for j in range(i - 19, i + 1)]
        mean = sum(recent) / 20
        gains = sum(max(close[j] - close[j - 1], 0.0) for j in range(i - 13, i + 1))
        losses = sum(max(close[j - 1] - close[j], 0.0) for j in range(i - 13, i + 1))
        rsi = gains / (gains + losses) if gains + losses else 0.5
        vols = [float(x.volume) for x in bars[i - 9:i + 1]]
        avg_volume = sum(vols) / len(vols)
        values = (close[i] / close[i - 1] - 1,
                  close[i] / close[i - 3] - 1,
                  close[i] / close[i - 8] - 1,
                  close[i] / close[i - 20] - 1,
                  float((b.h - b.l) / b.c), float((b.c - b.o) / b.o),
                  float(b.volume) / avg_volume - 1 if avg_volume else 0.0,
                  sqrt(sum((x - mean)**2 for x in recent)/20), rsi)
        if not all(isfinite(v) for v in values):
            raise ValueError('Non-finite feature encountered')
        out.append(FeatureRow(i, b.open_ms, values))
    return out


def regime(row: FeatureRow) -> str:
    volatility = row.values[7]
    momentum = row.values[3]
    if volatility > 0.02:
        return 'HIGH_VOLATILITY'
    if abs(momentum) > 0.03:
        return 'TRENDING_UP' if momentum > 0 else 'TRENDING_DOWN'
    return 'LOW_MOMENTUM'


def is_closed(bars: Sequence[Bar], now_ms: int) -> bool:
    return all(int(b.close_ms) < now_ms for b in bars)
