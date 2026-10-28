"""Admin-only operational evidence routes. Never authorize live trading."""
from __future__ import annotations
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from trader.api import authorized
from trader.operations.repository import OperationsRepository

router = APIRouter(prefix='/v1/operations', tags=['reliability / shadow readiness'])


def admin(claims: Annotated[dict, Depends(authorized)]):
    if claims.get('role') != 'ADMIN':
        raise HTTPException(403,'ADMIN required')
    return claims


@router.get('/readiness')
async def readiness(request: Request, claims: Annotated[dict, Depends(admin)]):
    return {'assessments':await OperationsRepository(request.app.state.db).recent(5),
            'live_trading_enabled':False}


@router.get('/incidents')
async def incidents(request: Request, claims: Annotated[dict, Depends(admin)]):
    return {'incidents':await OperationsRepository(request.app.state.db).incidents()}


@router.post('/evaluate')
async def evaluate_now(request: Request, claims: Annotated[dict, Depends(admin)]):
    repo = OperationsRepository(request.app.state.db)
    report = await repo.snapshot()
    await request.app.state.db.audit(str(claims['sub']),'ops.readiness.evaluated','recorded',
                                     {'hash':report['sha256'], 'verdict':report['verdict']})
    return report


class ResolveIncident(BaseModel):
    investigation: str = Field(min_length=20,max_length=1000)


@router.post('/incidents/{incident_id}/resolve')
async def resolve(incident_id: int,body:ResolveIncident,request:Request,
                  claims:Annotated[dict,Depends(admin)]):
    # A separate human investigation is mandatory; resolution does not arm an exchange.
    async with request.app.state.db.pool.connection() as conn:
        row = await (await conn.execute('''UPDATE operations_incidents
           SET resolved_at=now(),resolved_by=%s WHERE id=%s AND resolved_at IS NULL
           RETURNING id''',(int(claims['sub']),incident_id))).fetchone()
    if not row:
        raise HTTPException(404,'No open incident')
    await request.app.state.db.audit(str(claims['sub']),'ops.incident.resolved','accepted',
                                     {'incident_id':incident_id,'investigation':body.investigation})
    return {'id':incident_id,'resolved':True,'execution_armed':False}
