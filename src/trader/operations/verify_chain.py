"""Auditor command: verify complete append-only readiness evidence chain.

Run via operator-created database role with SELECT operations_readiness;
no mutation, secrets or trading permission required.
"""
from __future__ import annotations

import asyncio
import logging
from trader.config import Settings
from trader.operations.readiness import evidence_digest
from trader.storage import Storage


async def verify(db) -> int:
    prev = None
    count = 0
    last_id = 0
    async with db.pool.connection() as conn:
        while True:
            rows = await (await conn.execute('''SELECT id,evidence,sha256,prior_sha256
              FROM operations_readiness WHERE id>%s ORDER BY id LIMIT 500''',
              (last_id,))).fetchall()
            if not rows:
                break
            for id_,document,checksum,previous in rows:
                if previous != prev or evidence_digest(document,prev) != checksum:
                    raise RuntimeError(f'HASH_CHAIN_MISMATCH_AT_SNAPSHOT_{id_}')
                prev = checksum
                last_id = id_
                count += 1
    return count


async def main():
    logging.basicConfig(level=logging.INFO)
    store=Storage(Settings.load().database_url)
    await store.open()
    try:
        count=await verify(store)
        print(f'VALID_READINESS_HASH_CHAIN snapshots={count}')
    finally:
        await store.close()


if __name__=='__main__':
    asyncio.run(main())
