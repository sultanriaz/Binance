"""Safe, JSON-only linear probability model. No pickle or executable model loading."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from trader.intelligence.features import (
    FEATURE_NAMES,
    LEGACY_FEATURE_NAMES,
    MICROSTRUCTURE_FEATURE_NAMES,
    FeatureRow,
)

SUPPORTED_SCHEMAS = (LEGACY_FEATURE_NAMES, MICROSTRUCTURE_FEATURE_NAMES)


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha256(value: dict) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class LinearProbabilityModel:
    weights: tuple[float, ...]
    intercept: float
    means: tuple[float, ...]
    scales: tuple[float, ...]
    threshold: float
    feature_names: tuple[str, ...] = FEATURE_NAMES

    def __post_init__(self):
        if self.feature_names is None or self.feature_names == FEATURE_NAMES:
            if len(self.weights) == len(MICROSTRUCTURE_FEATURE_NAMES):
                object.__setattr__(self, 'feature_names', MICROSTRUCTURE_FEATURE_NAMES)
            else:
                object.__setattr__(self, 'feature_names', LEGACY_FEATURE_NAMES)
        n = len(self.feature_names)

        if (len(self.weights) != n or len(self.means) != n or len(self.scales) != n or
                tuple(self.feature_names) not in SUPPORTED_SCHEMAS):
            raise ValueError('Feature schema mismatch')
        if not (0.5 <= self.threshold <= 0.85):
            raise ValueError('Prediction threshold outside laboratory limits')
        if any(not math.isfinite(x) for x in (*self.weights, self.intercept,
                                                *self.means, *self.scales, self.threshold)):
            raise ValueError('Non-finite model parameters')
        if any(s <= 0 for s in self.scales):
            raise ValueError('Invalid scaler')

    def probability(self, row: FeatureRow) -> float:
        if len(row.values) != len(self.feature_names):
            raise ValueError('Invalid feature row')
        z = self.intercept + sum(w * ((x - mu) / scale) for w, x, mu, scale in zip(
            self.weights, row.values, self.means, self.scales))
        return 1 / (1 + math.exp(-max(-60, min(60, z))))

    def to_artifact(self) -> dict:
        body = {'format': 'aegistrade-linear-v1', 'feature_names': list(self.feature_names),
                'weights': list(self.weights), 'intercept': self.intercept,
                'means': list(self.means), 'scales': list(self.scales),
                'threshold': self.threshold}
        return {**body, 'sha256': sha256(body)}

    @classmethod
    def from_artifact(cls, artifact: dict) -> 'LinearProbabilityModel':
        body = {k: v for k, v in artifact.items() if k != 'sha256'}
        if body.get('format') != 'aegistrade-linear-v1' or sha256(body) != artifact.get('sha256'):
            raise ValueError('Untrusted or corrupted model artifact')
        if set(body) != {'format', 'feature_names', 'weights', 'intercept', 'means', 'scales', 'threshold'}:
            raise ValueError('Unsupported artifact fields')
        return cls(tuple(body['weights']), float(body['intercept']),
                   tuple(body['means']), tuple(body['scales']), float(body['threshold']),
                   tuple(body['feature_names']))
