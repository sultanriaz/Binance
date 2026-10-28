"""Optional DB-integrated paper smoke check (creates a VIRTUAL test account)."""
from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import os
import time
from trader.storage import Storage
from trader.paper.repository import PaperRepository
from trader.paper.domain import Quote
from trader.paper.engine import place_order


async def main():
    database=os.environ['DATABASE_URL']
    storage=Storage(database)
    await storage.open()
    try:
        repo=PaperRepository(storage)
        a=await repo.create_account('smoke-virtual-only',Decimal('10000'))
        ts=int(time.time()*1000)
        q=Quote('spot','BTCUSDT',Decimal('99'),Decimal('101'),Decimal('10000'),Decimal('10000'),ts,True)
        account,order=await repo.mutate(a.account_id,lambda state,p,conn:
           place_order(state,q,p,side='BUY',quantity=Decimal('2'),ts_ms=ts,
                       client_id='smoke-buy-00000001'))
        restored=await repo.read_account(a.account_id)
        assert restored and order['status']=='FILLED'
        assert restored[0].to_dict()==account.to_dict()
        assert restored[0].sequence==3  # initial deposit, acceptance, fill
        assert restored[0].cash<Decimal('10000')
        print(json.dumps({'status':'PASS','virtual_account_id':a.account_id,
                          'paper_order_id':order['id'],'virtual_cash':str(account.cash)}))
    finally:
        await storage.close()


if __name__=='__main__':
    asyncio.run(main())
