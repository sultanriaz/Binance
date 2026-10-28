"""Offline P26 deterministic fault suite. Never touches an exchange or live account.
Run: PYTHONPATH=src python scripts/ops/fault_drill.py
"""
from __future__ import annotations
import json
import sys
from copy import deepcopy
from trader.operations.readiness import evaluate


def baseline():
    return dict(database_healthy=True,collector_streams=[{'connected':True,'age_seconds':1}],
        worker_heartbeats=[{'service':x,'status':'HEALTHY','age_seconds':1}
         for x in ('collector','paper-worker','research-worker','outbox','operations-worker')],
        unresolved_market_gaps=0,open_exchange_incidents=0,unknown_exchange_orders=0,
        open_operations_incidents=0,backup_age_seconds=60,
        shadow_models=[dict(status='SHADOW',resolved_count=400,span_days=21,brier=.14,age_seconds=10)])


def main():
    controls=[
        ('DB_OUTAGE',lambda e:e.update(database_healthy=False)),
        ('WS_STALE',lambda e:e['collector_streams'][0].update(age_seconds=500)),
        ('WORKER_CRASH',lambda e:e['worker_heartbeats'][0].update(status='HALTED')),
        ('UNKNOWN_ORDER',lambda e:e.update(unknown_exchange_orders=1)),
        ('BACKUP_STALE',lambda e:e.update(backup_age_seconds=None)),
        ('INCIDENT',lambda e:e.update(open_operations_incidents=1)),
        ('DATA_GAP',lambda e:e.update(unresolved_market_gaps=1)),
        ('POOR_MODEL',lambda e:e['shadow_models'][0].update(brier=.5)),
        ('INSUFFICIENT_SAMPLE',lambda e:e['shadow_models'][0].update(resolved_count=10)),
        ('EARLY_MODEL',lambda e:e['shadow_models'][0].update(span_days=1)),
    ]
    result=[]
    good=evaluate(baseline())
    assert good['verdict']=='REVIEW_REQUIRED' and not good['live_trading_authorized']
    for name,mutator in controls:
        e=baseline()
        mutator(e)
        verdict=evaluate(e)
        passed=verdict['verdict']=='NO_GO' and bool(verdict['reasons']) and not verdict['live_trading_authorized']
        result.append({'fault':name,'passed':passed,'reasons':verdict['reasons']})
    print(json.dumps({'offline_faults':result,'passed':sum(r['passed'] for r in result),
                      'total':len(result),'exchange_requests':0},indent=2))
    return 0 if all(r['passed'] for r in result) else 1


if __name__=='__main__':
    sys.exit(main())
