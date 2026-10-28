"""Pure approval invariants, independent of persistence and HTTP handlers."""
from __future__ import annotations
import hashlib
import json


def digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


class ApprovalError(ValueError):
    pass


def can_review(*, requester: int, reviewer: int, status: str, expired: bool) -> None:
    if requester == reviewer:
        raise ApprovalError('Independent reviewer required')
    if status != 'PENDING':
        raise ApprovalError('Request already resolved')
    if expired:
        raise ApprovalError('Approval request expired')


def can_consume(*, status: str, expired: bool, actual: dict, expected_sha256: str) -> None:
    if status != 'APPROVED' or expired:
        raise ApprovalError('Valid approved unexpired request required')
    if digest(actual) != expected_sha256:
        raise ApprovalError('Approved payload differs from requested action')
