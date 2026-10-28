"""Fail-closed, evidence-based shadow readiness assessment.

No function in this module grants live-trading permission. REVIEW_REQUIRED is not GO.
All predicates are evaluated from durable operational and shadow data.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

POLICY_VERSION = 'P28-shadow-readiness-v1'


@dataclass(frozen=True)
class GatePolicy:
    min_days: int = 14
    min_resolved: int = 100
    max_brier: float = 0.25
    max_stream_age_seconds: int = 45
    max_heartbeat_age_seconds: int = 90
    max_model_age_seconds: int = 300

    def __post_init__(self):
        if not (7 <= self.min_days <= 180 and 30 <= self.min_resolved <= 200000):
            raise ValueError('Unsafe shadow sample requirements')
        if not (0 < self.max_brier <= 0.35):
            raise ValueError('Unsafe Brier threshold')


def evaluate(evidence: dict[str, Any], policy: GatePolicy = GatePolicy()) -> dict[str, Any]:
    """Return a stable report with every failure reason, never a tradable token."""
    reasons: list[str] = []
    required = ('database_healthy','collector_streams','worker_heartbeats',
                'unresolved_market_gaps','open_exchange_incidents',
                'unknown_exchange_orders','open_operations_incidents',
                'shadow_models','backup_age_seconds')
    for name in required:
        if name not in evidence:
            reasons.append(f'MISSING_{name.upper()}')
    if evidence.get('database_healthy') is not True:
        reasons.append('DATABASE_NOT_VERIFIED')
    streams = evidence.get('collector_streams') or []
    if not streams:
        reasons.append('NO_MARKET_STREAM_EVIDENCE')
    for stream in streams:
        if (not stream.get('connected') or stream.get('age_seconds') is None or
                stream['age_seconds'] < 0 or stream['age_seconds'] > policy.max_stream_age_seconds):
            reasons.append('STALE_MARKET_STREAM')
            break
    beats = evidence.get('worker_heartbeats') or []
    required_workers = {'collector','paper-worker','research-worker','outbox','operations-worker'}
    known = {x.get('service'): x for x in beats}
    for service in sorted(required_workers):
        row = known.get(service)
        if (row is None or row.get('status') != 'HEALTHY' or
            row.get('age_seconds') is None or row['age_seconds'] < 0 or
            row['age_seconds'] > policy.max_heartbeat_age_seconds):
            reasons.append('WORKER_UNHEALTHY_' + service.upper().replace('-', '_'))
    for key, reason in (('unresolved_market_gaps','MARKET_DATA_GAPS'),
                        ('open_exchange_incidents','EXCHANGE_INCIDENTS'),
                        ('unknown_exchange_orders','UNKNOWN_ORDER_OUTCOMES'),
                        ('open_operations_incidents','OPERATIONS_INCIDENTS')):
        value = evidence.get(key)
        if type(value) is not int or value < 0 or value > 0:
            reasons.append(reason)
    backup_age = evidence.get('backup_age_seconds')
    if backup_age is None or not isinstance(backup_age,(int,float)) or not math.isfinite(backup_age) or backup_age > 86400 or backup_age < 0:
        reasons.append('BACKUP_NOT_RECENTLY_VERIFIED')
    models = evidence.get('shadow_models') or []
    if not models:
        reasons.append('NO_APPROVED_SHADOW_MODEL')
    for m in models:
        if m.get('status') != 'SHADOW':
            reasons.append('UNAPPROVED_MODEL')
        if m.get('age_seconds') is None or not (0 <= m['age_seconds'] <= policy.max_model_age_seconds):
            reasons.append('STALE_SHADOW_OBSERVATIONS')
        if (type(m.get('resolved_count')) is not int or
                m['resolved_count'] < policy.min_resolved):
            reasons.append('INSUFFICIENT_RESOLVED_SHADOW_TRADES')
        if m.get('span_days') is None or m['span_days'] < policy.min_days:
            reasons.append('INSUFFICIENT_SHADOW_DURATION')
        brier = m.get('brier')
        if (brier is None or not isinstance(brier,(float,int)) or not math.isfinite(brier)
                or not (0 <= brier <= policy.max_brier)):
            reasons.append('SHADOW_CALIBRATION_NOT_ACCEPTABLE')
    reasons = sorted(set(reasons))
    return {
        'policy_version': POLICY_VERSION,
        'verdict': 'NO_GO' if reasons else 'REVIEW_REQUIRED',
        'reasons': reasons,
        'requirements': {'min_days': policy.min_days, 'min_resolved': policy.min_resolved,
                         'max_brier': policy.max_brier},
        'evidence': evidence,
        'live_trading_authorized': False,
        'human_approval_required': True,
        'notice': 'REVIEW_REQUIRED is NOT a trading GO. Release still needs separate authorization.'
    }


def canonical_json(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, default=str, allow_nan=False,
                      separators=(',', ':')).encode()


def evidence_digest(report: dict, previous: str | None) -> str:
    return hashlib.sha256(canonical_json({'report': report, 'previous': previous})).hexdigest()
