"""P32 read-only operational report; avoids invented net-PnL from incomplete data."""
from __future__ import annotations

async def live_report(db):
    async with db.pool.connection() as conn:
        rows=await (await conn.execute('''SELECT code,state,armed,automation_enabled,
            last_reconciled_at,equity_baseline,equity_baseline_day,last_error
            FROM live_accounts ORDER BY code''')).fetchall()
        report=[]
        for code,state,armed,auto,reconciled,baseline,day,error in rows:
            counts=await (await conn.execute('''SELECT status,COUNT(*) FROM live_order_intents
                    WHERE account_code=%s GROUP BY status''',(code,))).fetchall()
            fees=await (await conn.execute('''SELECT asset,SUM(fee) FROM live_fills
                    WHERE account_code=%s GROUP BY asset''',(code,))).fetchall()
            incidents=await (await conn.execute('''SELECT COUNT(*) FROM live_incidents
                      WHERE account_code=%s AND acknowledged_at IS NULL''',(code,))).fetchone()
            orders=await (await conn.execute('''SELECT COUNT(*) FROM live_order_intents
                     WHERE account_code=%s AND created_at>=date_trunc('day',now())
                       AND source!='PROTECTIVE' ''',(code,))).fetchone()
            report.append({'account':code,'state':state,'armed':armed,'automation':auto,
                           'last_reconciled_at':reconciled,'equity_baseline':str(baseline) if baseline is not None else None,
                           'baseline_date':str(day) if day else None,'error':error,
                           'statuses':dict(counts),'fees_by_asset':{a:str(f) for a,f in fees},
                           'open_incidents':incidents[0],'today_order_intents':orders[0]})
        latest=await (await conn.execute('''SELECT verdict,evaluated_at,sha256
            FROM operations_readiness ORDER BY id DESC LIMIT 1''')).fetchone()
    return {'accounts':report,'p28_readiness':{'verdict':latest[0],
            'evaluated_at':latest[1],'sha256':latest[2]} if latest else None,
            'limits':'Not a trading authorization or PnL assurance',
            'profitability':'Not calculated: reconciliation is not a cost-basis or cash-flow-adjusted performance engine'}
