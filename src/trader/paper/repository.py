"""Transactional PostgreSQL boundary for virtual portfolios and replay datasets.

One row lock serializes mutations per account. Account JSON + append-only ledger
are committed together; no Redis state is authoritative for balances/orders.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any, Callable, TypeVar
from uuid import uuid4

from trader.paper.domain import Account, Policy, dec, ds
from trader.paper.history import Bar

T = TypeVar('T')


def policy_dict(p: Policy) -> dict[str, Any]:
    return {k:ds(v) if isinstance(v,Decimal) else v for k,v in asdict(p).items()}


def policy_from_json(d: dict) -> Policy:
    decimal_keys={'max_order_notional','max_position_fraction','max_gross_exposure_fraction',
                  'max_daily_drawdown_fraction','max_total_drawdown_fraction','min_notional',
                  'taker_fee_fraction','maker_fee_fraction','slippage_bps','participation_fraction'}
    return Policy(**{k:dec(v) if k in decimal_keys else v for k,v in d.items()})


class PaperRepository:
    def __init__(self, storage):
        self.storage = storage

    async def create_account(self, name: str, initial_cash: Decimal,
                             policy: Policy | None = None) -> Account:
        from psycopg.types.json import Jsonb
        policy = policy or Policy()
        account=Account.create(str(uuid4()),initial_cash)
        account.audit('INITIAL_VIRTUAL_DEPOSIT',1,amount=ds(initial_cash),cash_after=ds(initial_cash))
        async with self.storage.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute('''INSERT INTO paper_accounts(id,name,state,policy)
                    VALUES (%s,%s,%s,%s)''',
                    (account.account_id,name,Jsonb(account.to_dict()),Jsonb(policy_dict(policy))))
                await conn.execute('INSERT INTO paper_ledger(account_id,seq,entry) VALUES (%s,%s,%s)',
                                   (account.account_id,account.sequence,Jsonb(account.ledger[-1])))
        return account

    async def list_accounts(self) -> list[dict]:
        async with self.storage.pool.connection() as conn:
            rows = await (await conn.execute('SELECT id,name,state,policy,updated_at FROM paper_accounts ORDER BY created_at DESC LIMIT 100')).fetchall()
            return [{'id':str(r[0]),'name':r[1], 'state':r[2],
                     'policy':r[3],'updated_at':r[4].isoformat()} for r in rows]

    async def read_account(self, account_id: str) -> tuple[Account,Policy] | None:
        async with self.storage.pool.connection() as conn:
            row = await (await conn.execute('SELECT state,policy FROM paper_accounts WHERE id=%s',
                                            (account_id,))).fetchone()
            return (Account.from_dict(row[0]),policy_from_json(row[1])) if row else None

    async def mutate(self, account_id: str,
                     fn: Callable[[Account, Policy, Any], T]) -> tuple[Account, T]:
        """fn runs synchronously while account row is locked; receives db connection.

        For complex DB queries inside callback, instead use the worker's explicit
        transaction. This method is intended for pure account mutations only.
        """
        async with self.storage.pool.connection() as conn:
            async with conn.transaction():
                row=await (await conn.execute('SELECT state,policy FROM paper_accounts WHERE id=%s FOR UPDATE',
                                              (account_id,))).fetchone()
                if row is None:
                    raise KeyError('ACCOUNT_NOT_FOUND')
                a=Account.from_dict(row[0])
                p=policy_from_json(row[1])
                previous=a.sequence
                result=fn(a,p,conn)
                await self._save(conn,a,previous)
        return a,result

    @staticmethod
    async def _save(conn, a: Account, old_seq: int) -> None:
        from psycopg.types.json import Jsonb
        if a.sequence != old_seq:
            for entry in a.ledger:
                if entry['seq'] > old_seq:
                    await conn.execute('INSERT INTO paper_ledger(account_id,seq,entry) VALUES (%s,%s,%s)',
                                       (a.account_id,entry['seq'],Jsonb(entry)))
        await conn.execute('UPDATE paper_accounts SET state=%s,updated_at=now() WHERE id=%s',
                           (Jsonb(a.to_dict()),a.account_id))

    async def bars(self, market: str, symbol: str, interval: str,
                   limit: int, end_ms: int | None = None) -> list[Bar]:
        if limit < 1 or limit > 50000:
            raise ValueError('invalid bar limit')
        async with self.storage.pool.connection() as conn:
            cur=await conn.execute('''SELECT market,symbol,interval,open_ms,close_ms,o,h,l,c,volume
                FROM historical_bars WHERE market=%s AND symbol=%s AND interval=%s
                AND (%s::bigint IS NULL OR open_ms < %s::bigint)
                ORDER BY open_ms DESC LIMIT %s''',
                (market,symbol,interval,end_ms,end_ms,limit))
            rows=list(await cur.fetchall())
        fields=('market','symbol','interval','open_ms','close_ms','o','h','l','c','volume')
        return [Bar.parse(dict(zip(fields,row))) for row in reversed(rows)]

    async def store_backtest(self, *, account_id: str | None, market: str, symbol: str,
                             interval: str, settings: dict, result: dict) -> str:
        from psycopg.types.json import Jsonb
        id_=str(uuid4())
        async with self.storage.pool.connection() as conn:
            await conn.execute('''INSERT INTO backtest_runs(id,account_id,market,symbol,interval,settings,result)
                VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                (id_,account_id,market,symbol,interval,Jsonb(settings),Jsonb(result)))
        return id_

    async def list_backtests(self, limit: int = 20) -> list[dict]:
        async with self.storage.pool.connection() as conn:
            rows=await (await conn.execute('''SELECT id,market,symbol,interval,
                result->>'total_return_fraction',result->>'max_drawdown_fraction',created_at
                FROM backtest_runs ORDER BY created_at DESC LIMIT %s''',(limit,))).fetchall()
        return [dict(id=str(x[0]),market=x[1],symbol=x[2],interval=x[3],
                     return_fraction=x[4],drawdown_fraction=x[5],created_at=x[6].isoformat()) for x in rows]
