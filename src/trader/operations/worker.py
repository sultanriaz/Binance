"""Read-only shadow/operations watcher. Never changes trading mode or order policy."""
from __future__ import annotations

import asyncio
import logging
import os

from trader.config import Settings
from trader.operations.repository import OperationsRepository
from trader.storage import Storage

LOG = logging.getLogger(__name__)


async def run_cycle(repo: OperationsRepository) -> dict:
    # Mark heartbeat before evidence collection. Failing DB => missing heartbeat => NO_GO.
    await repo.heartbeat('operations-worker')
    evidence = await repo.evidence()
    streams = evidence['collector_streams']
    for market in ('spot','futures'):
        row = next((r for r in streams if r['market'] == market), None)
        stale = row is None or not row['connected'] or (row['age_seconds'] or 1e9) > 45
        if stale:
            await repo.open_incident('market-data', f'{market.upper()}_STREAM_STALE')
    # Open operational incidents never close automatically: explicit investigation is required.
    return await repo.snapshot()


async def main():
    logging.basicConfig(level=logging.INFO)
    if os.getenv('ENABLE_LIVE_TRADING','false').lower() != 'false':
        raise RuntimeError('Operations worker cannot run with live trading enabled')
    db = Storage(Settings.load().database_url)
    await db.open()
    repo = OperationsRepository(db)
    try:
        while True:
            try:
                result = await run_cycle(repo)
                LOG.info('readiness=%s hash=%s', result['verdict'], result['sha256'][:12])
            except Exception:
                LOG.exception('Readiness evaluation failed; no clearance issued')
            await asyncio.sleep(60)
    finally:
        await db.close()


if __name__ == '__main__':
    asyncio.run(main())
